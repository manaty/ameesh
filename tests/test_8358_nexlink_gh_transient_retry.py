# SPDX-License-Identifier: AGPL-3.0-only
"""#8358: the Nexlink cutoff survives a GitHub 5xx and a secondary limit.

The incident: the 11ci cutoff stopped twice. At 10:24Z `gh run list` answered
`HTTP 504` and at 10:41Z `gh pr create` answered `GraphQL: was submitted too
quickly (createPullRequest)`. Both failed on the first attempt
(`after 1 attempt(s)`), the tag was already pushed, and every `ameesh-release`
tick (one per minute) repeated the same refusal until the secondary limit
expired.

The release wrapper now:
* replays a 502/503/504 with a short exponential wait, in the same tick;
* replays a write only when the forge state proves it did not happen (a lost
  response may hide an applied write), and returns the proven result instead;
* never replays a secondary limit: the original refusal keeps its
  `Retry-After`, `release._quota_wait_until` stores the wait, and the next
  ticks skip the engine until the window ends.
"""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from tests import support  # noqa: F401 - isolate host config before product imports
from ameesh import config
from ameesh import release
from ameesh import release_cutoff as rc
from ameesh.modules.nexlink import release as nx


#: The shape `GH_DEBUG=api` gives a 504: the status lives on the `< ` trace
#: line, and gh's final error names the HTTP status and the URL.
GH_DEBUG_504 = """* Request at 2026-11-27 10:24:00.000000000 +0000 UTC
* Request to https://api.github.com/repos/manaty/nexlink/actions/runs
> GET /repos/manaty/nexlink/actions/runs HTTP/1.1
> Authorization: token ████████████████████

< HTTP/2.0 504 Gateway Timeout
< Content-Type: application/json; charset=utf-8
< X-Ratelimit-Limit: 5000
< X-Ratelimit-Remaining: 4900
< X-Ratelimit-Reset: 1795562000

{"message":"Server Error"}
gh: HTTP 504: Gateway Timeout (https://api.github.com/repos/manaty/nexlink/actions/runs)
"""

#: #8358: a GraphQL secondary limit is an HTTP 200 carrying `errors` and,
#: when GitHub gives one, `Retry-After`. The status alone never classifies it.
GH_DEBUG_GRAPHQL_LIMIT = """* Request at 2026-11-27 10:41:00.000000000 +0000 UTC
* Request to https://api.github.com/graphql
> POST /graphql HTTP/1.1
> Authorization: token ████████████████████

< HTTP/2.0 200 OK
< Content-Type: application/json; charset=utf-8
< Retry-After: 30

{"data":{"createPullRequest":null},"errors":[{"type":"RATE_LIMITED",
"message":"was submitted too quickly (createPullRequest)"}]}
gh: was submitted too quickly (createPullRequest)
"""

#: Same refusal without `Retry-After`: the shared detector's default window.
GH_DEBUG_GRAPHQL_LIMIT_NO_RETRY = (
    GH_DEBUG_GRAPHQL_LIMIT.replace("< Retry-After: 30\n", ""))

#: A replay whose first response was lost can race GitHub's duplicate check.
GH_DEBUG_422_EXISTS = """< HTTP/2.0 422 Unprocessable Entity
< Content-Type: application/json

{"message":"A pull request already exists for manaty:release/11ci."}
gh: A pull request already exists for manaty:release/11ci (HTTP 422)
"""

GRAPHQL_LIMIT_TEXT = ("Nexlink GitHub operation failed: gh pr create -R "
                      "manaty/nexlink --head release/11ci --base main: HTTP 200 "
                      "after 1 attempt(s): was submitted too quickly "
                      "(createPullRequest)")
GRAPHQL_LIMIT_BODY = ('{"errors":[{"type":"RATE_LIMITED","message":"was '
                      'submitted too quickly (createPullRequest)"}]}')


def project():
    return rc.ProjectRelease("nexlink", "/unused", target="develop",
                             github="manaty/nexlink", model="nexlink")


def healthy_probe(reset: float = 1200.0):
    """A recharged `/rate_limit`: the wait must never need this probe."""
    body = ('{"resources": {"core": {"limit": 5000, "used": 100, '
            '"remaining": 4900, "reset": %d}}}' % int(reset))
    return 200, {"X-RateLimit-Limit": "5000", "X-RateLimit-Remaining": "4900",
                 "X-RateLimit-Reset": str(int(reset)),
                 "X-RateLimit-Resource": "core"}, body


class FakeGh:
    """A `plan_github.Gh` whose replies are consumed in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def factory(self, *args, **kwargs):
        self.kwargs = kwargs
        return self

    def run(self, args, stdin=None):
        self.calls.append(list(args))
        return self.replies.pop(0)


class Transient5xxTest(unittest.TestCase):
    @contextlib.contextmanager
    def fake_gh(self, replies):
        fake = FakeGh(replies)
        err = io.StringIO()
        with mock.patch.object(rc.plan_github, "Gh", fake.factory), \
                mock.patch.object(nx.time, "sleep") as sleep, \
                contextlib.redirect_stderr(err):
            yield fake, sleep, err

    def test_reads_and_writes_are_classified_before_a_retry(self):
        self.assertFalse(nx._gh_write(["run", "list"]))
        self.assertFalse(nx._gh_write(["pr", "view", "42"]))
        self.assertFalse(nx._gh_write(["pr", "list", "--json", "url"]))
        self.assertFalse(nx._gh_write(["api", "repos/acme/nexlink/actions/runs"]))
        self.assertTrue(nx._gh_write(["pr", "create", "--head", "release/x"]))
        self.assertTrue(nx._gh_write(["workflow", "run", "ci.yml"]))
        self.assertTrue(nx._gh_write(["pr", "merge", "42"]))
        self.assertTrue(nx._gh_write(
            ["api", "repos/acme/nexlink", "-X", "POST"]))

    def test_a_504_on_a_read_is_replayed_in_the_same_pass(self):
        with self.fake_gh([(1, "", GH_DEBUG_504),
                           (0, "[]", "")]) as (fake, sleep, err):
            result = nx.gh(project(), ["run", "list", "-R", "manaty/nexlink"])
        self.assertEqual(result, "[]")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [nx.GH_TRANSIENT_RETRY_DELAYS[0]])
        log = err.getvalue()
        self.assertIn("HTTP 504 on attempt 1/4", log)
        self.assertIn("gh run list", log)
        self.assertIn("recovered on attempt 2/", log)

    def test_a_persistent_504_names_the_status_and_the_attempts(self):
        attempts = len(nx.GH_TRANSIENT_RETRY_DELAYS) + 1
        with self.assertRaises(rc.TransientReleaseError) as caught:
            with self.fake_gh([(1, "", GH_DEBUG_504)] * attempts) \
                    as (fake, sleep, _err):
                nx.gh(project(), ["run", "list", "-R", "manaty/nexlink"])
        error = caught.exception
        self.assertEqual(error.github_refusal[0], 504)
        self.assertIn("HTTP 504 after %d attempt(s)" % attempts, str(error))
        self.assertNotIn("< HTTP", str(error))
        self.assertEqual(len(fake.calls), attempts)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         list(nx.GH_TRANSIENT_RETRY_DELAYS))

    def test_a_write_is_not_replayed_without_a_reconciled_state(self):
        with self.assertRaises(rc.TransientReleaseError) as caught:
            with self.fake_gh([(1, "", GH_DEBUG_504)]) as (fake, sleep, _err):
                nx.gh(project(), ["workflow", "run", "ci.yml"])
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()
        self.assertEqual(caught.exception.github_refusal[0], 504)

    def test_a_504_on_a_pr_view_is_replayed_in_the_same_pass(self):
        with self.fake_gh([(1, "", GH_DEBUG_504),
                           (0, '{"state":"OPEN"}', "")]) as (fake, sleep, _err):
            result = nx.gh(project(), ["pr", "view", "42", "--json", "state"])
        self.assertEqual(json.loads(result), {"state": "OPEN"})
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [nx.GH_TRANSIENT_RETRY_DELAYS[0]])

    def test_a_504_on_a_merge_is_resolved_by_the_read_back(self):
        with self.fake_gh([(1, "", GH_DEBUG_504)]) as (fake, sleep, _err):
            result = nx.gh(
                project(), ["pr", "merge", "42", "--squash",
                            "--match-head-commit", "a" * 40],
                reconcile=lambda: "merged")
        self.assertEqual(result, "merged")
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()

    def test_a_write_is_replayed_only_when_the_forge_proves_it_did_not_run(self):
        states = iter([None])

        def reconcile():
            return next(states, "https://github.com/manaty/nexlink/pull/42")

        with self.fake_gh([(1, "", GH_DEBUG_504),
                           (0, "https://github.com/manaty/nexlink/pull/42", "")]) \
                as (fake, sleep, _err):
            result = nx.gh(project(), ["pr", "create", "--head", "release/11ci"],
                           reconcile=reconcile)
        self.assertIn("pull/42", result)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [nx.GH_TRANSIENT_RETRY_DELAYS[0]])

    def test_a_write_proven_done_is_returned_without_any_replay(self):
        with self.fake_gh([(1, "", GH_DEBUG_504)]) as (fake, sleep, _err):
            result = nx.gh(
                project(), ["pr", "create", "--head", "release/11ci"],
                reconcile=lambda: "https://github.com/manaty/nexlink/pull/42")
        self.assertEqual(result, "https://github.com/manaty/nexlink/pull/42")
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()

    def test_an_unreadable_reconciliation_is_not_a_replay(self):
        def reconcile():
            raise rc.TransientReleaseError("pr list unreadable")

        with self.fake_gh([(1, "", GH_DEBUG_504)]) as (fake, sleep, _err):
            with self.assertRaises(rc.TransientReleaseError) as caught:
                nx.gh(project(), ["pr", "create", "--head", "release/11ci"],
                      reconcile=reconcile)
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()
        self.assertIn("HTTP 504 after 1 attempt(s)", str(caught.exception))

    def test_a_duplicate_pr_is_resolved_by_the_list_not_reopened(self):
        url = "https://github.com/manaty/nexlink/pull/42"
        with self.fake_gh([(1, "", GH_DEBUG_422_EXISTS)]) as (fake, sleep, _err):
            result = nx.gh(
                project(), ["pr", "create", "--head", "release/11ci"],
                reconcile=lambda: url)
        self.assertEqual(result, url)
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()


class PublishReconciliationTest(unittest.TestCase):
    def test_a_504_on_pr_create_reads_the_created_pr_before_replaying(self):
        sha = "a" * 40
        pending = {"sha": sha, "tag": "release-11ci", "version": "11ci"}
        replies = [(0, "[]", ""),                       # no PR before opening
                   (1, "", GH_DEBUG_504),               # the create's response is lost
                   (0, json.dumps([{"url": "https://github.com/manaty/nexlink/pull/42"}]),
                    "")]                                 # but the forge has it
        with self.fake_gh(replies) as (fake, sleep, _err), \
                mock.patch.object(nx, "ensure_ref"), \
                mock.patch.object(rc, "remote_tag", return_value=sha):
            url = nx.publish(project(), pending)
        self.assertEqual(url, "https://github.com/manaty/nexlink/pull/42")
        self.assertEqual([call[:2] for call in fake.calls],
                         [["pr", "list"], ["pr", "create"], ["pr", "list"]])
        sleep.assert_not_called()

    @contextlib.contextmanager
    def fake_gh(self, replies):
        fake = FakeGh(replies)
        err = io.StringIO()
        with mock.patch.object(rc.plan_github, "Gh", fake.factory), \
                mock.patch.object(nx.time, "sleep") as sleep, \
                contextlib.redirect_stderr(err):
            yield fake, sleep, err


class DispatchReconciliationTest(unittest.TestCase):
    def _pending(self):
        return {"sha": "a" * 40, "version": "11ci",
                "integration": "integration/11ci", "main": "b" * 40,
                "ci_parts": [{"workflow": "mobile-android-release.yml"}]}

    def _row(self, now: float):
        return {"headSha": "a" * 40, "headBranch": "integration/11ci",
                "event": "workflow_dispatch",
                "createdAt": datetime.datetime.fromtimestamp(
                    now, datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                "status": "queued"}

    @contextlib.contextmanager
    def fake_gh(self, replies):
        fake = FakeGh(replies)
        err = io.StringIO()
        with mock.patch.object(rc.plan_github, "Gh", fake.factory), \
                mock.patch.object(nx.time, "sleep") as sleep, \
                contextlib.redirect_stderr(err):
            yield fake, sleep, err

    def test_a_504_dispatch_is_found_by_its_intent_and_not_resent(self):
        pending = self._pending()
        save = mock.Mock()
        with self.fake_gh([(1, "", GH_DEBUG_504)]) as (fake, sleep, _err), \
                mock.patch.object(nx, "runs", return_value=[self._row(1000.0)]):
            again = nx.workflow_step(
                project(), pending, "mobile-android-release.yml",
                staging=False, save=save, now=1000.0)
        self.assertFalse(again)
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()
        self.assertTrue(
            pending["dispatches"]["ci/mobile-android-release.yml"]["accepted"])
        save.assert_called()

    def test_a_504_dispatch_with_no_visible_run_is_not_resent(self):
        pending = self._pending()
        with self.fake_gh([(1, "", GH_DEBUG_504)]) as (fake, sleep, _err), \
                mock.patch.object(nx, "runs", return_value=[]):
            with self.assertRaises(rc.TransientReleaseError):
                nx.workflow_step(
                    project(), pending, "mobile-android-release.yml",
                    staging=False, save=mock.Mock(), now=1000.0)
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()
        self.assertNotIn("accepted",
                         pending.get("dispatches", {}).get(
                             "ci/mobile-android-release.yml", {}))


class GraphQLSecondaryLimitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.cfg = config.Config(state_dir=self.temp.name)

    def _error(self):
        error = rc.TransientReleaseError(GRAPHQL_LIMIT_TEXT)
        error.github_refusal = (
            200, {"Retry-After": "30"}, GRAPHQL_LIMIT_BODY)
        return error

    @contextlib.contextmanager
    def fake_gh(self, replies):
        fake = FakeGh(replies)
        err = io.StringIO()
        with mock.patch.object(rc.plan_github, "Gh", fake.factory), \
                mock.patch.object(nx.time, "sleep") as sleep, \
                contextlib.redirect_stderr(err):
            yield fake, sleep, err

    def test_a_graphql_secondary_limit_is_never_replayed(self):
        with self.assertRaises(rc.TransientReleaseError) as caught:
            with self.fake_gh([(1, "", GH_DEBUG_GRAPHQL_LIMIT)]) as (fake, sleep, _err):
                nx.gh(project(), ["pr", "create", "--head", "release/11ci"])
        error = caught.exception
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()
        self.assertEqual(error.github_refusal[0], 200)
        self.assertEqual(error.github_refusal[1]["Retry-After"], "30")
        self.assertIn("HTTP 200 after 1 attempt(s)", str(error))
        self.assertIn("submitted too quickly", str(error))

    def test_the_cut_waits_for_the_retry_after_without_a_new_call(self):
        error = self._error()
        probe = mock.Mock(return_value=healthy_probe())
        with mock.patch.object(release, "_quota_probe", probe):
            until = release._quota_wait_until(
                self.cfg, str(error), 1000.0, error.github_refusal)
        self.assertEqual(until, 1030.0)
        probe.assert_not_called()
        # The same window, one second later: the stored wait answers alone.
        with mock.patch.object(release, "_quota_probe") as second:
            self.assertEqual(
                release._quota_wait_until(
                    self.cfg, str(error), 1001.0, error.github_refusal),
                1030.0)
            second.assert_not_called()

    def test_the_refusal_body_routes_the_graphql_bucket(self):
        self.assertEqual(
            release._quota_probe_resource(
                "gh: was submitted too quickly (createPullRequest)",
                (200, {}, GRAPHQL_LIMIT_BODY)),
            "graphql")
        # The body alone names the GraphQL limit when the error line does not.
        self.assertEqual(
            release._quota_probe_resource(
                "Nexlink GitHub operation failed: gh pr create: refused",
                (200, {}, GRAPHQL_LIMIT_BODY)),
            "graphql")
        # A 200 body without the GraphQL marker never becomes a quota wait.
        self.assertIsNone(release._quota_probe_resource(
            "Nexlink GitHub operation failed: gh api rate_limit: HTTP 200",
            (200, {}, '{"data":{"viewer":{"login":"x"}}}')))

    def test_a_secondary_limit_without_retry_after_blocks_the_default_window(self):
        from ameesh.modules.dev import plan_github as pg
        reads = pg.SharedReads(self.temp.name, priority="gate")
        # A stale resource header never moves the GraphQL refusal elsewhere.
        until = reads.note({"X-RateLimit-Resource": "core"}, status=200,
                           now=1000.0, resource="graphql",
                           body=GRAPHQL_LIMIT_BODY)
        self.assertEqual(until, 1000.0 + pg.RATE_LIMIT_WAIT)
        self.assertEqual(reads.blocked(1001.0, "graphql"),
                         1000.0 + pg.RATE_LIMIT_WAIT)

    def test_a_graphql_limit_without_retry_after_still_waits(self):
        status, headers, body = rc.plan_github.debug_refusal(
            GH_DEBUG_GRAPHQL_LIMIT_NO_RETRY)
        self.assertEqual(status, 200)
        self.assertNotIn("Retry-After", headers)
        with mock.patch.object(release, "_quota_probe") as probe:
            until = release._quota_wait_until(
                self.cfg, GRAPHQL_LIMIT_TEXT, 1000.0, (status, headers, body))
        from ameesh.modules.dev import plan_github as pg
        self.assertEqual(until, 1000.0 + pg.RATE_LIMIT_WAIT)
        probe.assert_not_called()

    def test_the_cut_reports_once_then_the_next_tick_skips_the_engine(self):
        error = self._error()
        rc.save_state(self.cfg, {"schema": rc.SCHEMA, "projects": {"nexlink": {}}})
        send = mock.Mock()
        with mock.patch.object(rc, "compiled_rules",
                               return_value=(None, types.SimpleNamespace(commit="d" * 40))), \
                mock.patch.object(rc, "cutoff_slot", return_value=(1000.0, "23:00")), \
                mock.patch.object(release, "_quota_probe") as probe, \
                mock.patch.object(rc, "send_report", send):
            waited = release._report_error(
                self.cfg, object(), project(), error, 1000.0)
        self.assertTrue(waited)
        probe.assert_not_called()
        send.assert_called_once()
        self.assertFalse(send.call_args.kwargs["urgent"])
        self.assertEqual(send.call_args.args[4]["result"], "waiting-quota")
        slot = rc.load_state(self.cfg)["projects"]["nexlink"]
        self.assertEqual(slot["quota_wait_reset"], 1030.0)
        self.assertEqual(slot["quota_wait_until"], 1090.0)

        engine = types.SimpleNamespace(
            execute=mock.Mock(return_value={"result": "waiting-ci"}))
        with mock.patch.object(rc, "release_provider", return_value=engine):
            report = rc.execute(self.cfg, None, project(), now=1089.0)
        self.assertEqual(report["result"], "waiting-quota")
        engine.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
