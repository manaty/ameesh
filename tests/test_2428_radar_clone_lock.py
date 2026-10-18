# SPDX-License-Identifier: AGPL-3.0-only
"""#2428 : un essai stacké concurrent ne change plus l'arbre lu par une fusion.

Le clone du radar est partagé par le passage radar, les atterrissages et les
commandes d'un tour (`work land`, `work queue add`). Un verrou de worktree
(`flock`, réentrant dans un processus) sérialise les opérations qui lisent ou
écrivent son arbre : un essai concurrent attend ou diffère, il ne sort jamais
la cible sous les gates d'une fusion (migration 521 listée puis absente).
"""
from __future__ import annotations

import dataclasses
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ameesh import merge_queue, radar, resolutions

from .test_l160b_radar import _git
from .test_l160e2_land import _Land


class CloneLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2428-lock-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.replayer = resolutions.Replayer(os.path.join(self.tmp, "repo"),
                                             os.path.join(self.tmp, "clone"))

    def test_nested_acquisition_in_one_thread(self):
        with self.replayer.locked(wait=0):
            with self.replayer.locked(wait=0):
                pass

    def test_another_thread_cannot_enter(self):
        seen = []

        def other():
            try:
                with self.replayer.locked(wait=0.05):
                    seen.append("entered")
            except resolutions.CloneBusy:
                seen.append("busy")

        with self.replayer.locked():
            thread = threading.Thread(target=other)
            thread.start()
            thread.join(timeout=5)
        self.assertEqual(seen, ["busy"])
        # released: the same lock is available again
        with self.replayer.locked(wait=0.05):
            pass

    def test_another_process_cannot_enter(self):
        """The bug crossed process boundaries (a radar pass and a turn's
        command): the exclusion must be an OS lock, not only a thread lock."""
        child = (
            "import sys, time\n"
            "from ameesh import resolutions\n"
            "with resolutions.Replayer('', sys.argv[1]).locked(wait=10):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(10)\n"
        )
        root = os.path.dirname(os.path.dirname(os.path.abspath(resolutions.__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (root, env.get("PYTHONPATH")) if part)
        proc = subprocess.Popen([sys.executable, "-c", child, self.replayer.path],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "held",
                             proc.stderr.read() if proc.poll() is not None else "")
            with self.assertRaises(resolutions.CloneBusy):
                with self.replayer.locked(wait=0.05):
                    pass
        finally:
            proc.kill()
            proc.communicate(timeout=10)


class ConcurrentTrialTest(_Land):
    def test_a_stacked_trial_cannot_change_the_tree_a_gate_reads(self):
        marker = os.path.join(self.tmp, "gate-started")
        release = os.path.join(self.tmp, "gate-release")
        self.project = dataclasses.replace(
            self.project,
            gates=("printf started > %s; n=0; "
                   "while [ ! -f %s ] && [ \"$n\" -lt 400 ]; do sleep 0.05; "
                   "n=$((n + 1)); done; grep -qx y other.py"
                   % (shlex.quote(marker), shlex.quote(release)),))
        self.queued(4)
        clone = resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p"))
        target = _git(self.origin, "rev-parse", "main")
        out: dict = {}
        errors: list = []

        def landing():
            try:
                out.update(self.land(4))
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                errors.append(exc)

        thread = threading.Thread(target=landing)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not os.path.exists(marker) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(os.path.exists(marker), "the landing gate never started")
            # Without the lock, this checkout removes the merged `other.py`
            # under the running gate: the gate fails and the lot is ejected.
            with mock.patch.object(resolutions, "CLONE_LOCK_WAIT_S", 0.05):
                with self.assertRaises(resolutions.CloneBusy):
                    merge_queue.stack(clone, target, [], [])
        finally:
            with open(release, "w"):
                pass
            thread.join(timeout=20)
        self.assertFalse(thread.is_alive(), "the landing did not finish")
        self.assertFalse(errors, errors)
        self.assertEqual(out.get("result"), "landed", out)
        self.assertEqual(_git(self.origin, "rev-parse", "main"), out.get("sha"))


class BusyTrialTest(_Land):
    def test_a_busy_clone_defers_without_ejecting_nor_caching(self):
        self.queued(4)
        cache = radar.Cache()
        tips = {self.fake.lots[4]["branch"]: self.tip(4),
                "main": _git(self.origin, "rev-parse", "main")}
        with mock.patch.object(merge_queue, "stack",
                               side_effect=resolutions.CloneBusy("worktree busy")), \
                mock.patch.object(merge_queue, "fetch_fresh",
                                  side_effect=lambda repo, names, clone: (dict(tips), repo)):
            out = merge_queue.run_pass(self.cfg, None, self.project, {}, cache=cache)
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued", out)
        self.assertEqual([(one["lot"], one["state"]) for one in out["targets"]["main"]],
                         [(4, "queued")])
        self.assertIn("CloneBusy", out.get("error") or "")
        self.assertEqual(self.sent, [])
        # nothing cached: the next pass runs the trial again
        self.assertEqual(cache.stacks, {})
