# SPDX-License-Identifier: AGPL-3.0-only
"""#6854 : un `Tracker` construit sans son fil `fond` (Runner en mode `once`,
tests) ne rend pas le processus sous-moissonneur.

Récidive de #5541 : `Runner.__init__` construit le `Tracker`, dont
`__init__` appelait `become_subreaper()`. Chaque module de tests qui
construisait un Runner faisait de `scripts/test-parts.py` un
sous-moissonneur, et personne ne moissonnait les orphelins adoptés (`sh`,
`sort`) : 471 zombies sous runner-ameesh-cx3 le 11/11. Le scénario tourne
dans un processus à part, pour que le processus de tests ne soit jamais
lui-même sous-moissonneur.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

from . import support  # noqa: F401
from ameesh import platform

SCENARIO = r"""
import ctypes, os, subprocess, sys, time, threading
from types import SimpleNamespace
from ameesh import background, platform
runner = SimpleNamespace(db=None, host="h", harness_pids=set(),
                         launch_lock=threading.Lock(), container_runtime=lambda: None)
tracker = background.Tracker(runner, log=lambda _: None)
flag = ctypes.c_int(-1)
ctypes.CDLL(None).prctl(37, ctypes.byref(flag), 0, 0, 0)  # PR_GET_CHILD_SUBREAPER
# un shell lance un petit-enfant en fond puis sort : le petit-enfant est orphelin
subprocess.run(["sh", "-c", "sleep 0.1 & exit 0"], check=True)
time.sleep(0.6)
zombies = [r["pid"] for r in platform.children() if r["zombie"]]
print(tracker.subreaper, flag.value, len(zombies))
"""


@unittest.skipUnless(platform.is_linux(), "PR_SET_CHILD_SUBREAPER : Linux")
class TrackerSubreaperTest(unittest.TestCase):
    def test_tracker_sans_fil_fond_ne_garde_pas_de_zombie(self):
        out = subprocess.run([sys.executable, "-c", SCENARIO], check=True,
                             capture_output=True, text=True, timeout=30,
                             env=dict(os.environ)).stdout.split()
        self.assertEqual(out, ["False", "0", "0"],
                         "sous-moissonneur sans moisson : zombies adoptés")

    def test_release_rend_les_orphelins_a_init(self):
        self.assertTrue(platform.become_subreaper())
        support.release_orphans()
        import ctypes
        flag = ctypes.c_int(-1)
        ctypes.CDLL(None).prctl(37, ctypes.byref(flag), 0, 0, 0)
        self.assertEqual(flag.value, 0)


if __name__ == "__main__":
    unittest.main()
