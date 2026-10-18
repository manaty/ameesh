# SPDX-License-Identifier: AGPL-3.0-only
"""#2328 : commande → description de l'étiquette → porte réelle du workflow.

Une cible avance sans push de tête : le payload garde l'ancienne base, mais le
merge-ref utilise la nouvelle. Les refus restent effectifs sur les deux parents.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.test_l161g_ci_gel import BRANCHE, DEPOT, Gh, Github, ci, pull


class TargetBaseWorkflowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ci-target-base-")
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        self.write("base.txt", "old base\n")
        self.old = self.commit()
        self.git("checkout", "-qb", BRANCHE)
        self.write("lot.txt", "frozen head\n")
        self.head = self.commit()
        self.git("checkout", "-q", "main")
        self.write("base.txt", "new target tip\n")
        self.base = self.commit()
        self.git("merge", "-q", "--no-ff", BRANCHE, "-m", "merge-ref")
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/tests.yml").read_text()
        step = workflow.split("      - name: gel (", 1)[1].split("      # une poussée garde", 1)[0]
        self.script = textwrap.dedent(step.split("        run: |\n", 1)[1])

    def git(self, *args):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=self.repo,
                              env=self.env, text=True, capture_output=True,
                              check=True).stdout.strip()

    def write(self, path, content):
        (self.repo / path).write_text(content)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def gate(self, base):
        env = {**self.env, "AMEESH_CI_LABEL": "ci-lot-%s" % self.head,
               "AMEESH_CI_HEAD": self.head, "AMEESH_CI_BASE": base,
               "BASE_REF": "main", "HEAD_REF": BRANCHE,
               "GITHUB_OUTPUT": str(self.repo / "output"),
               "GITHUB_ENV": str(self.repo / "env")}
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", self.script],
                              cwd=self.repo, env=env, text=True, capture_output=True)

    def test_stale_payload_uses_current_target_through_the_label_event(self):
        api = Github(head=self.head, base=self.base,
                     pulls=[pull(head=self.head, base=self.old)])
        pr = ci._pr_of(api, DEPOT, BRANCHE)
        label = ci.label_name("lot", self.head)
        gh = Gh(repo_labels={label})
        self.assertTrue(ci._pose_request(gh, DEPOT, pr, label, "lot")["ok"])
        self.assertEqual(gh.posed_bases, [self.base])
        result = self.gate(gh.posed_bases[0])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("base=%s\n" % self.base, (self.repo / "output").read_text())

    def test_stale_or_unusable_requested_base_is_refused(self):
        for base in (self.old, "", "CI #1343 lot", "$(touch injected)"):
            with self.subTest(base=base):
                result = self.gate(base)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.repo / "output").exists())
        self.assertFalse((self.repo / "injected").exists())

    def test_a_different_merge_head_is_still_refused(self):
        self.git("checkout", "-q", "--detach", self.head)
        self.write("lot.txt", "different head\n")
        other = self.commit()
        self.git("checkout", "-q", "main^1")
        self.git("merge", "-q", "--no-ff", other, "-m", "wrong merge-ref")
        result = self.gate(self.base)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the merge-ref head is", result.stdout)
        self.assertFalse((self.repo / "output").exists())
