# SPDX-License-Identifier: AGPL-3.0-only
"""#3640: a Nexlink develop head whose tree is a green packet head's is green
for the cutoff (décision owner #141353 a, #3633 for ameesh) — real Git, fake CI."""
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from ameesh import nexlink_release as nx, release_cutoff as rc

SUBJECT = ("Merge #3654 « Nexlink package 180 on develop — #3645 (AM-0) » "
           "(integration/paquet-180-nexlink)")


def run(workflow, run_id, conclusion):
    return {"id": run_id, "url": "https://example/runs/%d" % run_id,
            "workflow": workflow, "event": "push", "conclusion": conclusion,
            "jobs": [{"name": "suite", "status": "completed", "conclusion": conclusion}]}


class Fixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "develop")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@localhost")
        self.base = self.commit("base", "base")
        self.project = rc.ProjectRelease("nexlink", str(self.repo), target="develop",
                                         github="acme/nexlink", model="nexlink")
        self.readings = {}
        self.suites = {}

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True,
                                       stderr=subprocess.STDOUT).strip()

    def commit(self, name, text):
        (self.repo / name).write_text(text)
        self.git("add", ".")
        self.git("commit", "-qm", name)
        return self.git("rev-parse", "HEAD")

    def packet(self, subject=SUBJECT, resolution=False):
        """develop = merge --no-ff of a packet branch; returns (packet head, merge)."""
        self.git("checkout", "-q", "-b", "integration/paquet-180-nexlink", self.base)
        head = self.commit("lot", "lot")
        self.git("checkout", "-q", "develop")
        self.git("merge", "-q", "--no-ff", "--no-commit", head)
        if resolution:
            (self.repo / "lot").write_text("resolved")
            self.git("add", ".")
        self.git("commit", "-qm", subject)
        return head, self.git("rev-parse", "HEAD")

    def ci(self, sha, green, *runs):
        self.readings[sha] = {"green": green, "acceptable": green}
        self.suites[sha] = {"head": sha, "runs": list(runs)}

    def proof(self, sha):
        with mock.patch.object(rc, "ci_reading", side_effect=lambda p, s: self.readings[s]), \
                mock.patch.object(rc, "suite_reading", side_effect=lambda p, s: self.suites[s]):
            return nx.tree_green(self.project, sha, self.readings[sha])


class TreeGreenTest(Fixture, unittest.TestCase):
    def test_red_head_inherits_the_green_packet_head(self):
        head, merge = self.packet()
        self.ci(head, True, run("Backend suite", 10, "success"), run("Docs gate", 11, "success"))
        self.ci(merge, False, run("Backend suite", 20, "failure"), run("Docs gate", 21, "success"))
        proof = self.proof(merge)
        self.assertEqual(proof["head"], head)
        self.assertEqual(proof["required"], ["Backend suite", "Docs gate"])
        self.assertEqual([r["id"] for r in proof["runs"]], [10, 11])
        self.assertEqual(proof["tree"], self.git("rev-parse", head + "^{tree}"))
        self.assertIn("tree of %s equals packet head %s" % (merge[:12], head[:12]),
                      nx.tree_proof_text(merge, proof))

    def test_cancelled_or_running_head_inherits(self):
        head, merge = self.packet()
        self.ci(head, True, run("Backend suite", 10, "success"))
        for conclusion in ("cancelled", ""):
            with self.subTest(conclusion=conclusion):
                self.ci(merge, False, run("Backend suite", 20, conclusion))
                self.assertEqual(self.proof(merge)["head"], head)

    def test_head_without_any_run_inherits(self):
        """Témoin auditeur (2) : une tête de develop sans run → héritage."""
        head, merge = self.packet()
        self.ci(head, True, run("Backend suite", 10, "success"))
        self.ci(merge, False)
        proof = self.proof(merge)
        self.assertEqual(proof["head"], head)
        self.assertEqual(proof["runs"], [])
        self.assertIn("no run on the head", nx.tree_proof_text(merge, proof))

    def test_workflow_red_on_develop_and_absent_on_packet_head_never_inherits(self):
        """Témoin auditeur (1) : la tête de paquet ne prouve pas ce workflow."""
        head, merge = self.packet()
        self.ci(head, True, run("Backend suite", 10, "success"))
        self.ci(merge, False, run("Backend suite", 20, "success"), run("Docs gate", 21, "failure"))
        self.assertIsNone(self.proof(merge))

    def test_skipped_run_on_develop_requires_nothing(self):
        head, merge = self.packet()
        self.ci(head, True, run("Backend suite", 10, "success"))
        self.ci(merge, False, run("Backend suite", 20, "failure"), run("lab", 21, "skipped"))
        self.assertEqual(self.proof(merge)["required"], ["Backend suite"])

    def test_older_success_does_not_redeem_a_newer_failed_attempt(self):
        head, merge = self.packet()
        self.ci(head, True, run("Backend suite", 10, "success"), run("Backend suite", 12, "failure"))
        self.ci(merge, False, run("Backend suite", 20, "failure"))
        self.assertIsNone(self.proof(merge))

    def test_packet_head_not_green_never_inherits(self):
        head, merge = self.packet()
        self.ci(head, False, run("Backend suite", 10, "success"))
        self.ci(merge, False, run("Backend suite", 20, "failure"))
        self.assertIsNone(self.proof(merge))

    def test_conflict_resolution_never_inherits(self):
        """Garde #2692 : l'arbre de la fusion doit être celui de ^2."""
        head, merge = self.packet(resolution=True)
        self.ci(head, True, run("Backend suite", 10, "success"))
        self.ci(merge, False, run("Backend suite", 20, "failure"))
        self.assertIsNone(nx.packet_head(str(self.repo), merge))
        self.assertIsNone(self.proof(merge))

    def test_only_a_packet_merge_inherits(self):
        head, merge = self.packet(subject="Merge branch 'feature' into develop")
        self.ci(head, True, run("Backend suite", 10, "success"))
        self.ci(merge, False, run("Backend suite", 20, "failure"))
        self.assertIsNone(self.proof(merge))

    def test_github_packet_subject_is_recognised(self):
        head, merge = self.packet(
            subject="Merge pull request #3654 from manaty/integration/paquet-180-nexlink")
        self.assertEqual(nx.packet_head(str(self.repo), merge), head)

    def test_green_head_needs_no_proof(self):
        head, merge = self.packet()
        self.ci(head, True, run("Backend suite", 10, "success"))
        self.ci(merge, True, run("Backend suite", 20, "success"))
        self.assertIsNone(self.proof(merge))


class PlanTest(Fixture, unittest.TestCase):
    """The cutoff preview takes the inheriting head as its base and says why."""

    def preview(self, merge, main):
        refs = {"origin/develop": merge, "origin/main": main}
        with mock.patch.object(rc, "fetch"), \
                mock.patch.object(rc, "commit_of", side_effect=lambda repo, ref: refs[ref]), \
                mock.patch.object(rc, "compiled_rules",
                                  return_value=({}, types.SimpleNamespace(commit="d" * 40))), \
                mock.patch.object(rc, "cutoff_slot", return_value=(1000, "11:00")), \
                mock.patch.object(nx, "choose_version", return_value="11w"), \
                mock.patch.object(nx, "previous_release", return_value=self.base), \
                mock.patch.object(rc, "ci_reading", side_effect=lambda p, s: self.readings[s]), \
                mock.patch.object(rc, "suite_reading", side_effect=lambda p, s: self.suites[s]):
            return nx.plan(None, self.project, now=1000)

    def test_red_head_is_the_base(self):
        head, merge = self.packet()
        self.ci(self.base, True)
        self.ci(head, True, run("Backend suite", 10, "success"))
        self.ci(merge, False, run("Backend suite", 20, "failure"))
        plan = self.preview(merge, self.base)
        self.assertTrue(plan.green)
        self.assertEqual(plan.base, merge)
        self.assertEqual(plan.tree_green["head"], head)
        self.assertIn("tree of %s" % merge[:12], plan.notes)

    def test_unproven_red_head_falls_back_to_last_green(self):
        head, merge = self.packet()
        self.ci(self.base, True)
        self.ci(head, True)
        self.ci(merge, False, run("Docs gate", 21, "failure"))
        plan = self.preview(merge, self.base)
        self.assertFalse(plan.green)
        self.assertEqual(plan.base, self.base)
        self.assertIsNone(plan.tree_green)
        self.assertNotIn("tree of", plan.notes)


if __name__ == "__main__":
    unittest.main()
