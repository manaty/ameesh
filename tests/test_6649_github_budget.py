# SPDX-License-Identifier: AGPL-3.0-only
"""Real quota samples, local admission, and persistence across processes."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ameesh import github_budget as budget, github_identity, packet_watch, plan_github
from .test_l848_github_quota import Clock, FakeGitHub, BASE, quota_headers


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.state = self.tmp.name
        self.reads = plan_github.SharedReads(self.state, clock=self.clock, caller="watch", env={
            "AMEESH_HOST": "host-a", "AMEESH_ROLE": "github-watch", "AGENT_MAIL_NAME": "watcher"})

    def sample(self, remaining=999, status=200):
        self.reads.note(quota_headers(remaining=str(remaining), reset=2000), status=status)

    def test_reserve_is_shared_but_gate_only_bypasses_reserve_not_exhaustion(self):
        self.sample()
        other = plan_github.SharedReads(self.state, clock=self.clock)
        gate = plan_github.SharedReads(self.state, clock=self.clock, priority="gate")
        self.assertEqual(other.blocked(), 2000)
        self.assertIsNone(gate.blocked())
        self.sample(0, 403)
        self.assertEqual(gate.blocked(), 2000)
        self.clock.now = 2001
        self.assertIsNone(other.blocked())
        self.assertEqual(budget.low_quotas(self.state, now=2001), [])

    def test_twenty_percent_is_not_yet_low_and_other_buckets_are_separate(self):
        self.sample(1000)
        self.assertIsNone(self.reads.blocked())
        self.assertEqual(budget.low_quotas(self.state, now=1000), [])
        self.sample(999)
        self.assertIsNone(self.reads.blocked(resource="graphql"))
        self.assertEqual(budget.low_quotas(self.state, now=1000)[0]["remaining"], 999)

    def test_http_consumers_are_distinct_from_cached_reads_and_expire(self):
        self.sample(4999)
        self.sample(4999, 304)
        self.reads.count("hits", 50)
        self.reads.flush()
        report = plan_github.quota_stats(self.state)
        records = list(report["consumers"]["0"].values())
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record["requests"], record["not_modified"]), (2, 1))
        self.assertEqual((record["caller"], record["role"], record["host"]),
                         ("watch", "github-watch", "host-a"))
        self.assertEqual(record["process"], os.getpid())
        self.clock.now = 25 * 3600
        self.sample()
        self.assertEqual(list(plan_github.quota_stats(self.state)["consumers"]), ["25"])

    def test_reserve_returns_stale_metadata_or_inconclusive_without_network(self):
        fake = FakeGitHub(self.clock)
        fake.body["repos/o/r"] = {"ok": True}
        api = plan_github.Api("token", base=BASE, reads=self.reads, clock=self.clock, opener=fake)
        self.assertEqual(api.get("repos/o/r"), {"ok": True})
        self.clock.advance(100)
        self.sample()
        self.assertEqual(api.get("repos/o/r"), {"ok": True})
        self.assertTrue(api.last_read.stale)
        with self.assertRaises(plan_github.GithubRateLimited):
            api.get("repos/o/other")
        self.assertEqual(fake.calls, ["repos/o/r"])

    def test_raw_poll_admission_and_explicit_gate_use_the_same_sample(self):
        self.sample()
        env = {"AMEESH_GITHUB_STATE": self.state}
        self.assertIn("non-conclusive", budget.reserve_reason(["run", "list"], env=env, now=1000))
        self.assertIsNone(budget.reserve_reason(["pr", "merge", "2"], env=env, now=1000))
        self.assertIsNone(budget.reserve_reason(["run", "list"], env=dict(
            env, AMEESH_GITHUB_PRIORITY="gate"), now=1000))
        self.assertTrue(budget.is_poll(["api", "repos/o/r/actions/runs", "-X", "GET"]))
        self.assertFalse(budget.is_poll(["api", "repos/o/r/merges", "-X", "POST"]))
        self.assertFalse(budget.is_poll(["api", "graphql", "-f", "query=x"]))

    def test_gate_priority_reaches_the_gh_fallback_transport(self):
        self.sample()
        env = {"AMEESH_GITHUB_STATE": self.state}
        client = plan_github.client(env=env, gh=sys.executable, cache_dir=self.state,
                                    priority="gate", clock=self.clock)
        self.assertEqual(client.reads.priority, "gate")
        self.assertIsNone(budget.reserve_reason(["api", "repos/o/r"],
                                               env=client.gh.env, now=1000))
        self.assertNotIn("AMEESH_GITHUB_PRIORITY", env)

    def test_headerless_primary_403_defers_but_permission_403_does_not(self):
        self.reads.note({}, status=403, body="API rate limit exceeded for user")
        self.assertEqual(self.reads.blocked(), 1060)
        self.clock.now = 1061
        self.reads.note({}, status=403, body="Resource not accessible by integration")
        self.assertIsNone(self.reads.blocked())

    def test_generated_shim_refuses_poll_without_starting_gh(self):
        self.sample()
        # Real wall time for the subprocess, not the deterministic test clock.
        self.reads.note(quota_headers(remaining="100", reset=4_000_000_000), status=200)
        root = Path(self.state)
        real = root / "real-gh"
        real.write_text("#!/bin/sh\necho invoked\n")
        real.chmod(0o755)
        shim = github_identity.write_shim(str(root / "shim"), python=sys.executable)
        env = dict(os.environ, AMEESH_GH_REAL=str(real), AMEESH_GITHUB_STATE=self.state,
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        env.pop("AMEESH_GITHUB_PRIORITY", None)
        result = subprocess.run([shim, "run", "list"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 75)
        self.assertEqual(result.stdout, "")
        self.assertIn("non-conclusive", result.stderr)
        env["AMEESH_GITHUB_PRIORITY"] = "gate"
        result = subprocess.run([shim, "run", "list"], env=env, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "invoked")

    def test_packet_timer_spaces_forge_checks_across_restarts(self):
        watch = mock.Mock(app="app", repo="/checkout")
        cfg = mock.Mock()
        state = {}
        with mock.patch.object(packet_watch, "projects", return_value={"app": watch}), \
                mock.patch.object(packet_watch, "load_state", return_value=state), \
                mock.patch.object(packet_watch, "save_state"), \
                mock.patch.object(packet_watch, "check", return_value={}), \
                mock.patch.object(packet_watch, "_auto_merge", return_value=[]) as auto:
            for now in (1000, 1060, 1120, 1300):
                packet_watch.tick(cfg, None, now=now, rules_of=lambda name: {})
        self.assertEqual(auto.call_count, 2)
        self.assertEqual(state["auto_merge_checked_at"]["app"], 1300)
