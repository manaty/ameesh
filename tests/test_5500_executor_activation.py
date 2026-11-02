# SPDX-License-Identifier: AGPL-3.0-only
"""#5500: activation conditions of the elected staging executor.

Nothing read or run may be writable by another account, and the requested
commit's CI is read at the forge (anonymously) before any root operation.
No network, SSH, service or real deployment.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import urllib.error

from ameesh import release_cutoff, staging_executor as executor
from tests import test_4880_staging_executor as base


class _Root(unittest.TestCase):
    """A private root; the real parents above it are the host's business."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="5500-")
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        os.chmod(self.root, 0o700)
        self.addCleanup(os.umask, os.umask(0o022))
        judge = executor._entry_problem
        inside = lambda path: (path == self.root
                               or path.startswith(self.root + os.sep))
        patch = mock.patch.object(executor, "_entry_problem", side_effect=lambda path, st, owners: (
            judge(path, st, owners) if inside(path) else None))
        patch.start()
        self.addCleanup(patch.stop)

    def file(self, name, mode=0o600, text=""):
        path = Path(self.root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.write_text(text)
        os.chmod(path, mode)
        return str(path)


class PathGuardTest(_Root):
    def test_private_file_is_accepted(self):
        self.assertIsNone(executor.path_problem(self.file("bascule.env")))

    def test_group_or_other_writable_file_is_refused(self):
        for mode in (0o620, 0o602, 0o664):
            with self.subTest(mode=oct(mode)):
                self.assertIn("writable by group or others",
                              executor.path_problem(self.file("env", mode)))

    def test_writable_parent_is_refused(self):
        path = self.file("conf/staging.json")
        os.chmod(os.path.dirname(path), 0o770)
        self.assertIn("conf writable", executor.path_problem(path))

    def test_foreign_owner_is_refused(self):
        path = self.file("bascule.env")
        with mock.patch.object(executor, "_owners", return_value={0}):
            self.assertIn("owned by uid", executor.path_problem(path))

    def test_link_to_a_writable_target_is_refused(self):
        target = self.file("shared/real.env", 0o666)
        link = os.path.join(self.root, "bascule.env")
        os.symlink(target, link)
        self.assertIn("real.env writable", executor.path_problem(link))

    def test_missing_path_is_refused_unless_allowed(self):
        missing = os.path.join(self.root, "absent")
        self.assertIn("missing", executor.path_problem(missing))
        self.assertIsNone(executor.path_problem(missing, missing_ok=True))

    def test_tree_refuses_a_deep_writable_entry(self):
        self.file("clone/.git/hooks/post-checkout", 0o700)
        self.assertIsNone(executor.tree_problem(os.path.join(self.root, "clone")))
        os.chmod(os.path.join(self.root, "clone/.git/hooks/post-checkout"), 0o770)
        self.assertIn("post-checkout writable",
                      executor.tree_problem(os.path.join(self.root, "clone")))

    def test_tree_follows_its_links_to_their_target(self):
        clone = os.path.join(self.root, "clone")
        os.makedirs(clone, mode=0o700)
        os.symlink(self.file("outside/tool", 0o777), os.path.join(clone, "tool"))
        self.assertIn("tool writable", executor.tree_problem(clone))


class ActivationTest(_Root):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.root, "bin")
        os.makedirs(self.bin, mode=0o700)
        for name in ("git", "bash"):
            self.file("bin/" + name, 0o700)
        self.probe = self.file("probe", 0o700)
        os.makedirs(os.path.join(self.root, "clone"), mode=0o700)
        self.options = dict(repo=os.path.join(self.root, "clone"),
                            bascule_env=self.file("bascule.env"),
                            config_path=self.file("conf/staging.json"),
                            quiescence_probe=[self.probe])
        self.env = dict(PATH=self.bin)
        patch = mock.patch.object(executor, "startup_problem", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_private_setup_is_accepted(self):
        self.assertIsNone(executor.activation_problem(self.options, self.env))

    def test_each_element_is_guarded(self):
        cases = [("bascule", self.options["bascule_env"], "BASCULE_ENV"),
                 ("probe", self.probe, "program"),
                 ("git", os.path.join(self.bin, "git"), "program"),
                 ("PATH", self.bin, "PATH"),
                 ("clone", self.options["repo"], "clone")]
        for label, path, prefix in cases:
            with self.subTest(label):
                mode = os.stat(path).st_mode & 0o777
                os.chmod(path, mode | 0o020)
                try:
                    self.assertTrue(executor.activation_problem(
                        self.options, self.env).startswith(prefix + ": "))
                finally:
                    os.chmod(path, mode)

    def test_probe_script_argument_is_guarded(self):
        script = self.file("probe.sh", 0o666)
        self.options["quiescence_probe"] = ["bash", script]
        self.assertIn("probe.sh writable",
                      executor.activation_problem(self.options, self.env))

    def test_unresolved_program_or_relative_path_is_refused(self):
        self.options["quiescence_probe"] = ["prove-stopped"]
        self.assertIn("not found", executor.activation_problem(self.options, self.env))
        self.env["PATH"] = "bin:" + self.bin
        self.assertIn("relative entry",
                      executor.activation_problem(self.options, self.env))

    def test_startup_refusal_is_the_first_answer(self):
        with mock.patch.object(executor, "startup_problem", return_value="code: x"):
            self.assertEqual(executor.activation_problem(self.options, self.env), "code: x")


def run(sha, *, path=executor.TESTS_WORKFLOW, event="push", status="completed",
        conclusion="success", title="tests [main]", ident=1, name="tests"):
    return dict(head_sha=sha, path=path, event=event, status=status, name=name,
                conclusion=conclusion, display_title=title, id=ident,
                html_url="https://forge/run/%d" % ident)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class ForgeCiTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="5500-git-")
        self.addCleanup(tmp.cleanup)
        self.repo = tmp.name
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "integration/paquet-x")
        Path(self.repo, "f").write_text("lot\n")
        git(self.repo, "add", "f")
        git(self.repo, "commit", "-q", "-m", "lot")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "integration/paquet-x",
            "-m", "Merge pull request #7 from o/integration/paquet-x")
        self.merge = git(self.repo, "rev-parse", "HEAD")
        self.runs = {}

    def verdict(self, sha):
        return executor.forge_ci(self.repo, "o/r", sha,
                                 runs_of=lambda slug, commit: self.runs.get(commit, []))

    def test_own_green_ci_is_accepted(self):
        self.runs[self.base] = [run(self.base)]
        refusal, evidence = self.verdict(self.base)
        self.assertIsNone(refusal)
        self.assertIn(self.base, evidence)

    def test_red_absent_or_running_ci_is_refused(self):
        cases = {"red": [run(self.base, conclusion="failure")],
                 "red other workflow": [run(self.base), run(
                     self.base, path=".github/workflows/macos.yml", name="macos",
                     conclusion="failure", ident=2)],
                 "absent": [],
                 "scheduled only": [run(self.base, event="schedule")],
                 "running": [run(self.base, status="in_progress", conclusion=None)],
                 "cancelled": [run(self.base, conclusion="cancelled")]}
        for label, runs in cases.items():
            with self.subTest(label):
                self.runs[self.base] = runs
                refusal, _ = self.verdict(self.base)
                self.assertTrue(refusal.startswith(executor.CI_REFUSAL), refusal)

    def test_the_latest_run_of_a_workflow_decides(self):
        self.runs[self.base] = [run(self.base, conclusion="failure", ident=1),
                                run(self.base, ident=2)]
        self.assertIsNone(self.verdict(self.base)[0])

    def test_unreadable_forge_is_refused(self):
        def unreadable(slug, sha):
            raise executor.ForgeUnreadable("HTTP 403")
        refusal, _ = executor.forge_ci(self.repo, "o/r", self.base, runs_of=unreadable)
        self.assertEqual(refusal, executor.CI_REFUSAL + "unreadable (HTTP 403)")

    def test_tree_of_a_green_packet_head_is_green(self):
        self.runs[self.merge] = [run(self.merge, conclusion="failure")]
        self.runs[self.head] = [run(self.head, event="pull_request",
                                    title="tests [PR #7 ci-full-%s base x]" % self.head)]
        refusal, evidence = self.verdict(self.merge)
        self.assertIsNone(refusal)
        self.assertIn("packet head " + self.head, evidence)

    def test_packet_head_without_full_suite_or_green_is_refused(self):
        self.runs[self.merge] = [run(self.merge, status="queued", conclusion=None)]
        for label, head_runs in {
                "targeted": [run(self.head, event="pull_request",
                                 title="tests [PR #7 ci-lot-%s]" % self.head)],
                "red": [run(self.head, event="pull_request", conclusion="failure",
                            title="tests [PR #7 ci-full-%s]" % self.head)]}.items():
            with self.subTest(label):
                self.runs[self.head] = head_runs
                self.assertIsNotNone(self.verdict(self.merge)[0])

    def test_release_commit_inherits_its_first_parent(self):
        git(self.repo, "commit", "-q", "--allow-empty", "-m",
            "release: 1.2.3 [skip ci]\n\nameesh-release: 1.2.3")
        release = git(self.repo, "rev-parse", "HEAD")
        self.runs[self.merge] = [run(self.merge)]
        self.assertIsNone(self.verdict(release)[0])
        self.runs[self.merge] = [run(self.merge, conclusion="failure")]
        self.assertIsNotNone(self.verdict(release)[0])

    def test_ordinary_commit_never_inherits(self):
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "fix")
        self.runs[self.merge] = [run(self.merge)]
        self.assertIsNotNone(self.verdict(git(self.repo, "rev-parse", "HEAD"))[0])

    def test_forge_read_is_anonymous_and_closed_on_errors(self):
        captured = []

        class Response:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
            def read(self):
                return self.body

        def opener(request, timeout):
            captured.append(request)
            return Response(b'{"total_count": 1, "workflow_runs": [{"id": 1}]}')
        with mock.patch.object(executor.urllib.request, "urlopen", side_effect=opener):
            self.assertEqual(executor.forge_runs("o/r", "a" * 40), [{"id": 1}])
        self.assertTrue(captured[0].full_url.startswith(
            "https://api.github.com/repos/o/r/actions/runs?head_sha=" + "a" * 40))
        self.assertFalse({k.lower() for k in captured[0].headers} & {"authorization"})
        for failure in (urllib.error.HTTPError("u", 429, "quota", {}, None),
                        urllib.error.URLError("down"),
                        ValueError("json")):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(
                    executor.urllib.request, "urlopen", side_effect=failure):
                with self.assertRaises(executor.ForgeUnreadable):
                    executor.forge_runs("o/r", "a" * 40)
        truncated = b'{"total_count": 150, "workflow_runs": [{"id": 1}]}'
        with mock.patch.object(executor.urllib.request, "urlopen",
                               return_value=Response(truncated)):
            with self.assertRaises(executor.ForgeUnreadable):
                executor.forge_runs("o/r", "a" * 40)


class TickTest(unittest.TestCase):
    """The executor's tick under the simulated candidate of #4880."""
    setUp = base.ExecutorTest.setUp
    record = base.ExecutorTest.record
    recent = base.ExecutorTest.recent
    run_child = base.ExecutorTest.run_child

    def test_unsafe_activation_blocks_before_any_host_operation(self):
        self.request.update(state="running", host="host-a", run="attempt-a",
                            unit_pid=99)
        with mock.patch.object(executor, "activation_problem",
                               return_value="clone: x writable by group or others"), \
                mock.patch.object(release_cutoff, "_settle_poste_units") as settle:
            result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(self.request["blocked_reason"],
                         "unsafe activation: clone: x writable by group or others")
        self.assertEqual(self.calls, [])
        settle.assert_not_called()
        executor.subprocess.run.assert_not_called()

    def test_forge_refusal_queues_without_sequence_and_rereads_later(self):
        refusal = executor.CI_REFUSAL + "tests of aaaaaaaaaaaa in_progress"
        with mock.patch.object(executor, "forge_ci", return_value=(refusal, "")) as ci:
            result = executor.tick(self.db, self.cfg, self.options)
            self.assertEqual(result, {"state": "blocked", "reason": refusal})
            self.assertEqual(self.request["state"], "pending")
            self.assertEqual(self.calls, [])
            # Within CI_RECHECK_S the anonymous quota is spared.
            executor.tick(self.db, self.cfg, self.options)
            self.assertEqual(ci.call_count, 1)
            self.request["ci_checked"] = time.time() - executor.CI_RECHECK_S - 1
            executor.tick(self.db, self.cfg, self.options)
            self.assertEqual(ci.call_count, 2)
            # A forged future check time never suspends the reading.
            self.request["ci_checked"] = time.time() + 3600
            executor.tick(self.db, self.cfg, self.options)
            self.assertEqual(ci.call_count, 3)
        self.request["ci_checked"] = time.time() - executor.CI_RECHECK_S - 1
        result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "completed")
        self.assertNotIn("ci_checked", result)
        self.assertEqual(result["ci"], "CI green")

    def test_unsafe_worktree_requeues_without_running_its_script(self):
        with mock.patch.object(executor, "tree_problem", return_value="tree/x owned by uid 7"):
            result = executor.tick(self.db, self.cfg, self.options)
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(self.request["state"], "pending")
        self.assertIn("worktree", self.request["blocked_reason"])
        self.assertEqual(self.calls, [])
        release_cutoff._drop_worktree.assert_called_once()

    def test_settings_require_the_forge_slug(self):
        import json
        path = Path(self.root, "staging.json")
        data = dict(key="mesh", project="ameesh", role="pilot", candidates=["a", "b"],
                    repo=str(self.root), bascule_env=str(self.bascule),
                    config_path="/elsewhere")
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            executor.settings(str(path))
        for slug in ("o", "o/r/x", "o/r;rm"):
            path.write_text(json.dumps(dict(data, github=slug)))
            with self.subTest(slug), self.assertRaises(ValueError):
                executor.settings(str(path))
        path.write_text(json.dumps(dict(data, github="manaty/ameesh")))
        self.assertEqual(executor.settings(str(path))["config_path"], str(path))


class MainTest(unittest.TestCase):
    def test_unsafe_startup_refuses_before_reading_the_configuration(self):
        from ameesh import config
        with mock.patch("ameesh.platform.child_environment.harden", return_value=True), \
                mock.patch("ameesh.platform.child_environment.install"), \
                mock.patch.object(executor, "startup_problem", return_value="code: x"), \
                mock.patch.object(config, "load") as load, \
                mock.patch.object(executor.os, "umask") as umask:
            self.assertEqual(executor.main(["--config", "/c.json", "tick"]), 1)
        load.assert_not_called()
        umask.assert_called_once_with(0o022)


if __name__ == "__main__":
    unittest.main()
