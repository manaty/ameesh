# SPDX-License-Identifier: AGPL-3.0-only
"""L81 (#289) — resident rotation: continuous measurement and thresholds.

The measurement is read from `TurnResult.usage` (study §6.1) and the decision
is a pure function (study §6.2); the rotation itself runs on the L76 fake
harness (Claude Code stream-json) with Postgres, like `test_l76`.
"""
from __future__ import annotations

import json
import os
import sys
import time
import types
import unittest
from unittest import mock

from ameesh import adapters, cli, conversation, harnesses, mail, registry, resident_runner
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase
from .test_l75_resident import claude_doc


def _result(compact=None, **usage):
    """A finished turn's `TurnResult`, reduced to the usage it carries (and
    the native compaction it reported, L81 T3)."""
    return types.SimpleNamespace(usage=usage, compact=compact)


class TurnRereadTest(unittest.TestCase):
    def test_sums_the_three_counters_of_a_call(self):
        self.assertEqual(resident_runner.turn_reread(
            {"input_tokens": 10, "cache_read_input_tokens": 1000,
             "cache_creation_input_tokens": 5, "output_tokens": 999}), 1015)

    def test_absent_or_none_reads_as_zero(self):
        self.assertEqual(resident_runner.turn_reread(None), 0)
        self.assertEqual(resident_runner.turn_reread({}), 0)
        self.assertEqual(resident_runner.turn_reread({"input_tokens": None}), 0)


class RereadMaxTest(unittest.TestCase):
    def test_default_when_absent_or_unreadable(self):
        for value in (None, "", "   ", "abc", "-5", "nan"):
            self.assertEqual(resident_runner.reread_max(value), 50_000_000.0,
                             value)

    def test_reads_tokens_and_zero_disables(self):
        self.assertEqual(resident_runner.reread_max("12000000"), 12_000_000.0)
        self.assertEqual(resident_runner.reread_max("0"), 0.0)


class MaintenanceTest(unittest.TestCase):
    def test_rotation_on_the_context_ceiling(self):
        self.assertEqual(resident_runner.maintenance(
            context=15_000_000, window=200_000, reread=15_000_000,
            session_reread=15_000_000, cap=15_000_000), resident_runner.ROTATE)

    def test_the_turn_sum_alone_does_not_rotate_a_small_context(self):
        """#2336 : la somme des relectures du tour n'est pas une taille de
        contexte ; un petit contexte ne tourne pas parce que le tour a
        beaucoup d'étapes."""
        self.assertEqual(resident_runner.maintenance(
            context=1_000, window=200_000, reread=15_000_000,
            session_reread=15_000_000, cap=15_000_000), "")

    def test_alert_on_the_session_cumulative_reread(self):
        self.assertEqual(resident_runner.maintenance(
            context=1_000, window=200_000, reread=1_000,
            session_reread=50_000_000, reread_max=50_000_000),
            resident_runner.ALERT)

    def test_rotation_wins_over_compaction(self):
        self.assertEqual(resident_runner.maintenance(
            context=15_000_000, window=200_000, reread=15_000_000,
            session_reread=15_000_000, cap=15_000_000), resident_runner.ROTATE)

    def test_hard_ceiling_wins_over_the_cumulative_alert(self):
        self.assertEqual(resident_runner.maintenance(
            context=15_000_000, window=200_000, reread=15_000_000,
            session_reread=50_000_000, cap=15_000_000, reread_max=50_000_000),
            resident_runner.ROTATE)

    def test_the_alert_wins_over_compaction(self):
        self.assertEqual(resident_runner.maintenance(
            context=190_000, window=200_000, reread=1_000,
            session_reread=50_000_000, reread_max=50_000_000),
            resident_runner.ALERT)

    def test_compaction_at_half_the_window(self):
        self.assertEqual(resident_runner.maintenance(
            context=100_000, window=200_000, reread=1_000,
            session_reread=1_000), resident_runner.COMPACT)
        self.assertEqual(resident_runner.maintenance(
            context=99_999, window=200_000, reread=1_000, session_reread=1_000), "")

    def test_no_window_no_compaction(self):
        self.assertEqual(resident_runner.maintenance(
            context=10 ** 9, window=None, reread=1_000, session_reread=1_000), "")
        self.assertEqual(resident_runner.maintenance(
            context=None, window=200_000, reread=1_000, session_reread=1_000), "")

    def test_disabled_thresholds(self):
        # cap and reread_max at 0 are disabled: only the soft threshold speaks
        self.assertEqual(resident_runner.maintenance(
            context=190_000, window=200_000, reread=10 ** 9,
            session_reread=10 ** 9, cap=0, reread_max=0), resident_runner.COMPACT)
        self.assertEqual(resident_runner.maintenance(
            context=1_000, window=200_000, reread=10 ** 9,
            session_reread=10 ** 9, cap=0, reread_max=0), "")

    def test_share_zero_disables_compaction(self):
        self.assertEqual(resident_runner.maintenance(
            context=199_999, window=200_000, reread=1_000, session_reread=1_000,
            share=0), "")

    def test_compaction_does_not_loop_while_a_request_is_fresh(self):
        self.assertEqual(resident_runner.maintenance(
            context=100_000, window=200_000, reread=1_000, session_reread=1_000,
            since_compact=10, compact_retry_s=600), "")
        self.assertEqual(resident_runner.maintenance(
            context=100_000, window=200_000, reread=1_000, session_reread=1_000,
            since_compact=601, compact_retry_s=600), resident_runner.COMPACT)
        # a fresh request never masks a harder bound
        self.assertEqual(resident_runner.maintenance(
            context=15_000_000, window=200_000, reread=15_000_000,
            session_reread=15_000_000, cap=15_000_000,
            since_compact=10, compact_retry_s=600), resident_runner.ROTATE)


class ResidentMeasurementTest(unittest.TestCase):
    def _process(self) -> resident_runner.ResidentProcess:
        # measurement never touches the driver
        return resident_runner.ResidentProcess(object(), key=(), life_id="vie")

    def test_record_turn_accumulates(self):
        rp = self._process()
        rp.record_turn(_result(input_tokens=100, cache_read_input_tokens=900,
                               context_tokens=12_000, context_window=200_000))
        self.assertEqual((rp.last_context, rp.context_window), (12_000, 200_000))
        self.assertEqual((rp.last_reread, rp.session_reread), (1_000, 1_000))
        rp.record_turn(_result(input_tokens=50, cache_read_input_tokens=1_950,
                               context_tokens=40_000, context_window=200_000))
        self.assertEqual((rp.last_reread, rp.session_reread), (2_000, 3_000))
        self.assertEqual(rp.last_context, 40_000)

    def test_record_turn_without_usage_keeps_the_measure(self):
        rp = self._process()
        rp.record_turn(_result(context_tokens=12_000, context_window=200_000))
        rp.record_turn(_result())
        self.assertEqual((rp.last_context, rp.context_window), (12_000, 200_000))
        self.assertEqual((rp.last_reread, rp.session_reread), (0, 0))

    def test_maintenance_due_follows_the_last_turn(self):
        rp = self._process()
        self.assertEqual(rp.maintenance_due(), "")
        rp.record_turn(_result(context_tokens=150_000, context_window=200_000))
        self.assertEqual(rp.maintenance_due(), resident_runner.COMPACT)
        rp.record_turn(_result(input_tokens=15_000_000, context_tokens=150_000,
                               context_window=200_000))
        self.assertEqual(rp.maintenance_due(cap=150_000), resident_runner.ROTATE)

    def test_the_turn_sum_does_not_rotate_a_small_context(self):
        """#2336 : un tour qui a relu 15 M en plusieurs appels, dans une
        session de 1 000 jetons, ne franchit pas le plafond de 15 M."""
        rp = self._process()
        rp.record_turn(_result(input_tokens=15_000_000, context_tokens=1_000,
                               context_window=200_000))
        self.assertEqual(rp.maintenance_due(cap=15_000_000), "")

    def test_maintenance_due_reads_the_cumulative_reread(self):
        rp = self._process()
        rp.record_turn(_result(input_tokens=20_000_000, context_tokens=1_000,
                               context_window=200_000))
        self.assertEqual(rp.maintenance_due(reread_max=50_000_000), "")
        rp.record_turn(_result(input_tokens=31_000_000, context_tokens=1_000,
                               context_window=200_000))
        self.assertEqual(rp.maintenance_due(reread_max=50_000_000),
                         resident_runner.ALERT)

    def test_native_compaction_follows_the_after_and_holds_the_next_one(self):
        rp = self._process()
        rp.record_turn(_result(input_tokens=150_000, context_tokens=150_000,
                               context_window=200_000))
        self.assertEqual(rp.maintenance_due(), resident_runner.COMPACT)
        rp.compact_requested()
        self.assertEqual(rp.maintenance_due(), "")   # no loop while it lands
        rp.record_turn(_result(compact={"pre": 150_000, "post": 30_000},
                               input_tokens=150_000, context_tokens=150_000,
                               context_window=200_000))
        self.assertEqual(rp.compactions, 1)
        self.assertEqual((rp.compact_pre, rp.compact_post), (150_000, 30_000))
        self.assertEqual(rp.last_context, 30_000)    # the measure follows the after
        self.assertEqual(rp.maintenance_due(), "")


class ResidentBench(PgTestCase):
    """A worker over the fake resident harness, between two turns."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending, turn_resources CASCADE")
        self.log = os.path.join(self.tmp, "resident-trace.jsonl")
        path = os.path.join(self.tmp, "fake-claude.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(claude_doc(), fh)
        self.adapter = adapters.HarnessAdapter(harnesses.load(path, source="test"),
                                               sys.executable)
        for patch in (mock.patch.dict(os.environ, {"FAKE_RESIDENT_LOG": self.log}),
                      mock.patch.object(resident_runner, "binary_version",
                                        return_value="2.1.286")):
            patch.start()
            self.addCleanup(patch.stop)

    def _worker(self, name: str = "res81", **local):
        runner = Runner(self.cfg, self.db, once=False)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "claude", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        self.addCleanup(worker._close_resident, "end of test")
        worker._adapter = lambda: self.adapter
        self._set(worker, resident="on", **local)
        return runner, worker

    @staticmethod
    def _set(worker, **values) -> None:
        os.makedirs(worker.state_dir, exist_ok=True)
        for key, value in values.items():
            with open(os.path.join(worker.state_dir, key), "w", encoding="utf-8") as fh:
                fh.write(str(value))

    def _turn(self, worker, prompt: str) -> bool:
        return worker.run_turn({"kind": "prompt", "prompt": prompt, "ids": []})

    def _starts(self) -> list[list[str]]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(l)["argv"] for l in fh if l.strip() and '"argv"' in l]

    def _prompts(self) -> list[str]:
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return [json.loads(l)["user"] for l in fh if l.strip() and '"user"' in l]

    def _states(self, name: str = "res81") -> list[str]:
        page = conversation.history(self.db, name, limit=100)
        return [e["text"] for e in page["entries"] if e["kind"] == "state"]


class ResidentRotationTest(ResidentBench):
    """The rotation itself: measured between two turns, when quiet."""

    def test_cumulative_reread_rotates_between_two_turns(self):
        _runner, worker = self._worker(resident_reread_max="100")
        self.assertTrue(self._turn(worker, "premier"))
        rp = worker.resident
        self.assertIsNotNone(rp)
        # the fake harness answers each turn with usage 10+200+30
        self.assertEqual(rp.session_reread, 240)
        self.assertEqual(worker._resident_maintenance(), resident_runner.ALERT)
        self.assertTrue(worker.maybe_rotate())
        self.assertIsNone(worker.resident)          # the process was closed
        self.assertIsNone(worker.current_session())  # the session was forgotten
        self.assertTrue(worker.resume_summary)
        self.assertTrue(any("Session rotated" in t for t in self._states()))
        # the next turn opens a new session, from the resume summary
        self.assertTrue(self._turn(worker, "deux"))
        argv = self._starts()[-1]
        self.assertNotIn("--resume", argv)
        self.assertIn("Session resumed after rotation", self._prompts()[-1])

    def test_hard_ceiling_rotates_even_right_after_a_human_message(self):
        _runner, worker = self._worker(resident_reread_max="0")
        self.assertTrue(self._turn(worker, "premier"))
        worker._dernier_humain = time.monotonic()
        self.assertFalse(worker.maybe_rotate())      # nothing due yet
        self.assertIsNotNone(worker.resident)
        with mock.patch.object(worker, "context_max_tokens", return_value=100):
            self.assertEqual(worker._resident_maintenance(), resident_runner.ROTATE)
            self.assertTrue(worker.maybe_rotate())
        self.assertIsNone(worker.resident)

    def test_a_recent_human_message_holds_the_soft_rotation(self):
        _runner, worker = self._worker(resident_reread_max="100")
        self.assertTrue(self._turn(worker, "premier"))
        worker._dernier_humain = time.monotonic()
        self.assertFalse(worker.maybe_rotate())
        self.assertIsNotNone(worker.resident)        # the resident is kept
        self.assertIsNotNone(worker.current_session())
        worker._dernier_humain = time.monotonic() - resident_runner.QUIET_S - 1
        self.assertTrue(worker.maybe_rotate())
        self.assertIsNone(worker.resident)

    def test_never_policy_disables_rotation_but_keeps_native_compaction(self):
        _runner, worker = self._worker(resident_reread_max="100")
        self.assertTrue(self._turn(worker, "premier"))
        rp = worker.resident
        with mock.patch.object(worker, "session_policy", return_value="jamais"):
            for cap in (0, 100):
                with self.subTest(cap=cap), mock.patch.object(
                        worker, "context_max_tokens", return_value=cap):
                    self.assertEqual(worker._resident_maintenance(), "")
                    self.assertFalse(worker.maybe_rotate())
                    self.assertIs(worker.resident, rp)
            rp.last_context, rp.context_window = 600, 1000
            self.assertEqual(worker._resident_maintenance(), resident_runner.COMPACT)

    def test_half_the_window_compacts_between_two_turns(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_CONTEXT": "600",
                                          "FAKE_CLAUDE_WINDOW": "1000"}):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            rp = worker.resident
            self.assertEqual((rp.last_context, rp.context_window), (600, 1000))
            self.assertEqual(worker._resident_maintenance(), resident_runner.COMPACT)
            self.assertTrue(worker.maybe_rotate())
            self.assertEqual(rp.compactions, 1)
            self.assertEqual((rp.compact_pre, rp.compact_post), (600, 120))
            self.assertEqual(rp.last_context, 120)   # the measure follows the after
            self.assertEqual(worker._resident_maintenance(), "")   # no loop
            self.assertTrue(any("Session compacted" in t for t in self._states()))
            self.assertIn("/compact", self._prompts())

    def test_a_compaction_without_boundary_does_not_loop(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_CONTEXT": "600",
                                          "FAKE_CLAUDE_WINDOW": "1000",
                                          "FAKE_COMPACT_SKIP": "1"}):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            rp = worker.resident
            self.assertFalse(worker.maybe_rotate())  # one attempt, nothing landed
            self.assertEqual(rp.compactions, 0)
            self.assertEqual(self._prompts().count("/compact"), 1)
            self.assertEqual(worker._resident_maintenance(), "")  # cooldown
            self.assertFalse(worker.maybe_rotate())  # no second attempt
            self.assertEqual(self._prompts().count("/compact"), 1)
            self.assertTrue(any("no compact_boundary" in t for t in self._states()))

    def test_a_recent_human_message_holds_the_compaction(self):
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_CONTEXT": "600",
                                          "FAKE_CLAUDE_WINDOW": "1000"}):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            worker._dernier_humain = time.monotonic()
            self.assertFalse(worker.maybe_rotate())
            self.assertNotIn("/compact", self._prompts())
            worker._dernier_humain = time.monotonic() - resident_runner.QUIET_S - 1
            self.assertTrue(worker.maybe_rotate())
            self.assertEqual(worker.resident.compactions, 1)

    def test_a_waiting_human_message_holds_the_compaction(self):
        """B1 (#289) : l'entretien se décide avant le `pick` — un message
        humain en attente dans la boîte doit tenir la fenêtre de calme."""
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_CONTEXT": "600",
                                          "FAKE_CLAUDE_WINDOW": "1000"}):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            mid = mail.send(self.db, "proprio", worker.name, "fais le point",
                            payload={"human": True})
            self.assertFalse(worker._humain_recent())
            self.assertEqual(worker._resident_maintenance(), resident_runner.COMPACT)
            self.assertTrue(worker._humain_recent())      # l'attente est observée
            self.assertFalse(worker.maybe_rotate())       # ... et tient l'entretien
            self.assertNotIn("/compact", self._prompts())
            # remis, puis la fenêtre de calme passée : la compaction repart
            mail.mark_delivered(self.db, [mid])
            worker._dernier_humain = time.monotonic() - resident_runner.QUIET_S - 1
            self.assertEqual(worker._resident_maintenance(), resident_runner.COMPACT)
            self.assertTrue(worker.maybe_rotate())
            self.assertEqual(worker.resident.compactions, 1)

    def test_a_human_message_served_during_the_turn_holds_the_compaction(self):
        """B1 (#289) : un message humain remis par le hook PENDANT le tour
        n'est plus non-lu — c'est son instant, daté dans l'état du tour, qui
        tient la fenêtre de calme."""
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_CONTEXT": "600",
                                          "FAKE_CLAUDE_WINDOW": "1000"}):
            _runner, worker = self._worker(resident_reread_max="0")
            self.assertTrue(self._turn(worker, "premier"))
            cli._tour_remis(worker.cfg, worker.name, {"turn": "tour-1", "remis": 0},
                            [{"sender": "proprio", "payload": {"human": True}}])
            self.assertFalse(worker._humain_recent())
            self.assertEqual(worker._resident_maintenance(), resident_runner.COMPACT)
            self.assertTrue(worker._humain_recent())      # la remise datée est lue
            self.assertFalse(worker.maybe_rotate())
            self.assertNotIn("/compact", self._prompts())
            # la fenêtre de calme passée : l'entretien repart
            etat = cli.tour_lu(worker.cfg, worker.name)
            etat["humain"] = time.time() - resident_runner.QUIET_S - 1
            cli._tour_ecrit(worker.cfg, worker.name, etat)
            worker._dernier_humain = time.monotonic() - resident_runner.QUIET_S - 1
            self.assertFalse(worker._humain_recent())
            self.assertTrue(worker.maybe_rotate())
            self.assertEqual(worker.resident.compactions, 1)

    def test_the_session_cumulative_survives_a_sleep_wake(self):
        """B2 (#289) : le relu cumulé appartient à la session — endormi puis
        réveillé sur le même id, le résident reprend son cumul (240+240, et
        non 240 : le seuil de 400 ne s'élude pas)."""
        _runner, worker = self._worker(resident_reread_max="400")
        self.assertTrue(self._turn(worker, "premier"))
        premier = worker.resident
        self.assertIsNotNone(premier)
        self.assertEqual(premier.session_reread, 240)
        self.assertEqual(worker._resident_maintenance(), "")   # 240 < 400
        session = worker.current_session()
        self.assertIsNotNone(session)
        # L76 : le résident s'endort, la session est gardée pour le réveil
        worker._close_resident("asleep after 0s without a turn")
        self.assertIsNone(worker.resident)
        self.assertTrue(self._turn(worker, "deux"))
        rp = worker.resident
        self.assertIsNotNone(rp)
        self.assertIsNot(rp, premier)
        self.assertEqual(rp.session, session)        # la même session, reprise
        self.assertEqual(rp.session_reread, 480)     # 240 + 240, pas 240
        self.assertEqual(worker._resident_maintenance(), resident_runner.ALERT)


if __name__ == "__main__":
    unittest.main()
