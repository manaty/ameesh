# SPDX-License-Identifier: AGPL-3.0-only
"""L131 — heavy suites capped per host (`ameesh heavy`). Pure tests: slots
live in a temporary state directory, no database."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import adapters, heavy

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class SlotsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(state_dir=tempfile.mkdtemp(prefix="ameesh-l131-"),
                                   max_heavy_suites=2)
        env = mock.patch.dict(os.environ, {"AMEESH_MAX_HEAVY_SUITES": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_limit_from_config_then_cpus(self):
        self.assertEqual(heavy.limit(self.cfg), 2)
        with mock.patch.object(heavy.os, "cpu_count", return_value=12):
            self.assertEqual(heavy.limit(SimpleNamespace(max_heavy_suites=0)), 3)
        with mock.patch.object(heavy.os, "cpu_count", return_value=2):
            self.assertEqual(heavy.limit(SimpleNamespace(max_heavy_suites=0)), 1)

    def test_no_more_than_the_limit_at_once(self):
        first = heavy.acquire(self.cfg, command="jest")
        second = heavy.acquire(self.cfg, command="make build")
        self.assertEqual({first[0], second[0]}, {0, 1})
        logs = []
        third = heavy.acquire(self.cfg, command="pytest", log=logs.append,
                              sleep=lambda s: None, max_wait_s=0)
        self.assertIsNone(third)
        busy = heavy.status(self.cfg)
        self.assertEqual([s["busy"] for s in busy], [True, True])
        self.assertEqual({s["command"] for s in busy}, {"jest", "make build"})
        heavy.release(first[1])
        again = heavy.acquire(self.cfg, command="pytest", max_wait_s=0)
        self.assertIsNotNone(again)
        heavy.release(second[1])
        heavy.release(again[1])
        self.assertEqual([s["busy"] for s in heavy.status(self.cfg)], [False, False])

    def test_waiting_is_logged_with_the_holders(self):
        held = [heavy.acquire(self.cfg, command="jest --all") for _ in range(2)]
        logs, clock = [], iter(range(0, 1000, 30))
        self.assertIsNone(heavy.acquire(self.cfg, log=logs.append, sleep=lambda s: None,
                                        clock=lambda: next(clock), max_wait_s=100))
        self.assertTrue(logs)
        self.assertIn("2/2 heavy suites running", logs[0])
        self.assertIn("jest --all", logs[0])
        for _, fh in held:
            heavy.release(fh)

    def test_a_killed_holder_frees_its_slot(self):
        # the slot is a flock: the kernel drops it with the process
        env = dict(os.environ, PYTHONPATH=SRC, AMEESH_MAX_HEAVY_SUITES="1")
        code = ("import sys, types, time; from ameesh import heavy; "
                "cfg = types.SimpleNamespace(state_dir=sys.argv[1], max_heavy_suites=1); "
                "held = heavy.acquire(cfg, command='holder'); print('held', flush=True); "
                "time.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", code, self.cfg.state_dir],
                                stdout=subprocess.PIPE, text=True, env=env)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held")
            one = SimpleNamespace(state_dir=self.cfg.state_dir, max_heavy_suites=1)
            self.assertIsNone(heavy.acquire(one, max_wait_s=0))
            proc.kill()
            proc.wait(timeout=10)
            got = heavy.acquire(one, max_wait_s=0)
            self.assertIsNotNone(got)
            heavy.release(got[1])
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()

    def test_run_passes_the_exit_code_and_frees_the_slot(self):
        self.assertEqual(heavy.run(self.cfg, [sys.executable, "-c", "raise SystemExit(3)"],
                                   nice=0), 3)
        self.assertEqual(heavy.run(self.cfg, ["ameesh-no-such-command"], nice=0), 127)
        self.assertEqual([s["busy"] for s in heavy.status(self.cfg)], [False, False])


class NoteTest(unittest.TestCase):
    def test_mail_turns_tell_agents_to_use_ameesh_heavy(self):
        from ameesh import mail
        texte = mail._assemble("mail", ["bloc"], 0)
        self.assertIn(adapters.HEAVY_NOTE, texte)
        self.assertIn("ameesh heavy --", adapters.HEAVY_NOTE)


if __name__ == "__main__":
    unittest.main()
