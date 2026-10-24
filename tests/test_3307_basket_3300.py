# SPDX-License-Identifier: AGPL-3.0-only
"""#3307 — panier de #3300 (avis dev3 #17488, N/P3, tête gelée 2b7d5995).

(1) La relance d'inactivité annonçait les avis dus « before declaring yourself
available », mais les listait après « If you have nothing to do … stop » :
corrections et avis dus passent désormais avant la consigne de reprise.

(2) Les commandes de verdict proposées (reprise, alerte SLO `green_verdict`)
reprenaient le SHA du gel tel quel ; un gel historique de moins de 12
caractères y était refusé par `reviewed_head`. Il est complété en commit
entier dans un dépôt qui le connaît, sinon la commande dit `<frozen head>`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from ameesh import adapters, freeze_gate, reprise, runner, work
from ameesh import review_designation_activation as activation

ENV_GIT = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


class IdleOrderTest(unittest.TestCase):
    """Témoin (1) : les dus précèdent la phrase d'arrêt."""

    def prompt(self, fixes, reviews, pilot=""):
        worker = types.SimpleNamespace(db=None, name="r1", _idle_pilot=lambda: pilot)
        with mock.patch.object(reprise, "fixes_owed_lines", return_value=fixes), \
                mock.patch.object(reprise, "pending_review_lines", return_value=reviews):
            return runner.AgentWorker._idle_prompt(worker)

    def test_owed_reviews_and_fixes_come_before_the_stop(self):
        text = self.prompt(["- #7 fix"], ["- #9 review"], pilot="mesh-design")
        stop = text.index("If you have nothing to do")
        self.assertLess(text.index(adapters.IDLE_FIXES_HEADER),
                        text.index(adapters.IDLE_REVIEWS_HEADER))
        self.assertLess(text.index("- #9 review"), stop)
        self.assertLess(text.index("- #7 fix"), stop)
        self.assertTrue(text.endswith(adapters.idle_prompt("mesh-design")))

    def test_nothing_owed_keeps_the_plain_prompt(self):
        self.assertEqual(self.prompt([reprise.NO_FIX_OWED], [reprise.NO_PENDING_REVIEW]),
                         adapters.IDLE_PROMPT_NO_PILOT)


class VerdictShaTest(unittest.TestCase):
    """Témoin (2) : `--sha` d'une commande proposée, accepté par reviewed_head."""

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="ameesh-3307-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        for args in (("init", "-q"), ("commit", "-q", "--allow-empty", "-m", "c")):
            subprocess.run(["git", "-C", self.repo, *args], check=True, env=ENV_GIT,
                           capture_output=True)
        self.full = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"],
                                   check=True, capture_output=True,
                                   text=True).stdout.strip()
        patches = (mock.patch.object(work, "get", return_value={"id": 5}),
                   mock.patch.object(freeze_gate, "_head_repos",
                                     return_value=[self.repo]))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_long_frozen_head_is_kept(self):
        self.assertEqual(freeze_gate.verdict_sha(None, 5, self.full[:12].upper()),
                         self.full[:12])
        self.assertEqual(freeze_gate.verdict_sha(None, 5, self.full), self.full)

    def test_a_short_historical_head_is_completed(self):
        self.assertEqual(freeze_gate.verdict_sha(None, 5, self.full[:8]), self.full)

    def test_an_unresolved_short_head_gives_no_sha(self):
        self.assertEqual(freeze_gate.verdict_sha(None, 5, "deadbee1"), "")

    def test_the_owed_review_line_carries_an_accepted_sha(self):
        rows = [{"lot": 5, "state": "qa", "title": "t", "sha": self.full[:8],
                 "designated_ts": None},
                {"lot": 6, "state": "qa", "title": "u", "sha": "deadbee1",
                 "designated_ts": None}]
        with mock.patch.object(activation, "pending_reviews", return_value=rows):
            lines = reprise.pending_review_lines(None, "r1")
        self.assertIn("verdict ok|blocked --sha %s`" % self.full, lines[0])
        self.assertIn("verdict ok|blocked --sha <frozen head>`", lines[1])


if __name__ == "__main__":
    unittest.main()
