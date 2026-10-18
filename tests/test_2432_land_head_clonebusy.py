# SPDX-License-Identifier: AGPL-3.0-only
"""#2432 : `work land` (land_head) sur un clone radar occupé — CloneBusy.

#2428 sérialise le worktree du clone radar ; son test couvre l'essai stacké
(`merge_queue.stack`), pas l'atterrissage direct par la file (`land_head`).
Ici le verrou est tenu par un autre thread : `land_head` rend une erreur
CloneBusy, laisse la ligne `queued` sans éjection ni courrier, libère le bail
de la cible, puis le passage suivant atterrit normalement.
"""
from __future__ import annotations

import threading
import unittest
from unittest import mock

from ameesh import radar, resolutions

from .test_l160b_radar import _git
from .test_l160e2_land import _Land


class LandHeadBusyTest(_Land):
    def test_busy_clone_defers_land_head_then_the_next_pass_lands(self):
        self.queued(4)
        clone = resolutions.Replayer(self.repo, radar.clone_dir(self.cfg, "p"))
        held = threading.Event()
        release = threading.Event()

        def hold():
            with clone.locked():
                held.set()
                release.wait(10)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(5), "the clone lock was never held")
        try:
            with mock.patch.object(resolutions, "CLONE_LOCK_WAIT_S", 0.05):
                out = self.land(4)
        finally:
            release.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "the lock holder did not finish")

        # CloneBusy is reported as a retryable error, never an ejection.
        self.assertEqual(out["result"], "error", out)
        self.assertIn("CloneBusy", out["error"])
        self.assertEqual(self.fake.queue_rows[4]["state"], "queued")
        self.assertIn("retried", self.fake.queue_rows[4]["reason"])
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.locks.held["p:main"][0])  # lease freed for the retry

        # Nothing was written: the next pass lands the same frozen head.
        out = self.land(4)
        self.assertEqual(out["result"], "landed", out)
        self.assertEqual(_git(self.origin, "rev-parse", "main^2"), self.tip(4))


if __name__ == "__main__":
    unittest.main()
