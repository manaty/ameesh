# SPDX-License-Identifier: AGPL-3.0-only
"""#8256: a transient GitHub 401 is replayed before a Nexlink cutoff fails.

The incident: on 24/11 the final-tree CI was green and the tick was polling
its runs when `gh` answered `HTTP 401: Bad credentials`, twice (10:40Z for
11cd, 22:42Z for 11ce). `gh auth status` was valid seconds later and the next
tick resumed, but the failed tick exited non-zero and mailed an urgent event.

The release wrapper now replays a 401 with a backoff that spans the observed
refusals, and journalises the command and the HTTP status. A refusal whose
status is not 401 (a quota 403, a 404, an unreadable trace) is never replayed.
"""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from tests import support  # noqa: F401 - isolate host config before product imports
from ameesh import release_cutoff as rc
from ameesh.modules.nexlink import release as nx

#: The shape `GH_DEBUG=api` gives a failed `gh` call: the HTTP status lives on
#: the `< ` trace line, and the last plain line is gh's help hint — exactly
#: why the journal used to show only "Try authenticating ..." (#8256).
GH_DEBUG_401 = """* Request at 2026-11-24 22:42:33.123456789 +0000 UTC
* Request to https://api.github.com/repos/manaty/nexlink/actions/runs
> GET /repos/manaty/nexlink/actions/runs?per_page=100 HTTP/1.1
> Authorization: token ████████████████████

< HTTP/2.0 401 Unauthorized
< Content-Type: application/json; charset=utf-8
< X-Ratelimit-Limit: 5000
< X-Ratelimit-Remaining: 4999
< X-Ratelimit-Reset: 1795562000

{"message":"Bad credentials","documentation_url":"https://docs.github.com/rest"}
HTTP 401: Bad credentials (https://api.github.com/repos/manaty/nexlink/actions/runs)
Try authenticating with:  gh auth refresh -h github.com
"""

GH_DEBUG_429 = """< HTTP/2.0 429 Too Many Requests
< Retry-After: 30

{"message":"You have exceeded a secondary rate limit."}
gh: You have exceeded a secondary rate limit (HTTP 429)
"""


def project():
    return rc.ProjectRelease("nexlink", "/unused", target="develop",
                             github="manaty/nexlink", model="nexlink")


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


class AuthRetryTest(unittest.TestCase):
    @contextlib.contextmanager
    def fake_gh(self, replies):
        fake = FakeGh(replies)
        err = io.StringIO()
        with mock.patch.object(rc.plan_github, "Gh", fake.factory), \
                mock.patch.object(nx.time, "sleep") as sleep, \
                contextlib.redirect_stderr(err):
            yield fake, sleep, err

    def test_a_transient_401_is_replayed_until_the_api_answers(self):
        with self.fake_gh([(1, "", GH_DEBUG_401),
                           (0, '[{"databaseId": 1}]', "")]) as (fake, sleep, err):
            result = nx.gh(project(), ["run", "list", "-R", "manaty/nexlink"])
        self.assertEqual(json.loads(result), [{"databaseId": 1}])
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         [nx.GH_AUTH_RETRY_DELAYS[0]])
        log = err.getvalue()
        self.assertIn("gh run list", log)
        self.assertIn("HTTP 401 on attempt 1/%d" % (len(nx.GH_AUTH_RETRY_DELAYS) + 1), log)
        self.assertIn("recovered on attempt 2/", log)
        # The wrapper still asks gh for the original response (#8182).
        self.assertEqual(fake.kwargs["env"]["GH_DEBUG"], "api")

    def test_a_persistent_401_names_the_command_status_and_attempts(self):
        attempts = len(nx.GH_AUTH_RETRY_DELAYS) + 1
        with self.assertRaises(rc.TransientReleaseError) as caught:
            with self.fake_gh([(1, "", GH_DEBUG_401)] * attempts) as (fake, sleep, err):
                nx.gh(project(), ["run", "list", "-R", "manaty/nexlink"])
        error = caught.exception
        self.assertEqual(error.github_refusal[0], 401)
        text = str(error)
        self.assertIn("HTTP 401 after %d attempt(s)" % attempts, text)
        self.assertIn("gh run list", text)
        self.assertIn("gh auth refresh", text)   # the actionable hint is kept
        self.assertNotIn("< HTTP", text)         # never the debug trace
        self.assertEqual(len(fake.calls), attempts)
        self.assertEqual([call.args[0] for call in sleep.call_args_list],
                         list(nx.GH_AUTH_RETRY_DELAYS))
        self.assertIn("HTTP 401 on attempt 4/", err.getvalue())

    def test_another_status_is_never_replayed(self):
        with self.assertRaises(rc.TransientReleaseError) as caught:
            with self.fake_gh([(1, "", GH_DEBUG_429)]) as (fake, sleep, _err):
                nx.gh(project(), ["run", "list"])
        self.assertEqual(caught.exception.github_refusal[0], 429)
        self.assertIn("HTTP 429 after 1 attempt(s)", str(caught.exception))
        self.assertIn("secondary rate limit", str(caught.exception).lower())
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()

    def test_an_unreadable_status_is_not_replayed(self):
        with self.fake_gh([(1, "",
                            "Try authenticating with:  gh auth refresh -h github.com\n")]) \
                as (fake, sleep, _err):
            with self.assertRaises(rc.TransientReleaseError):
                nx.gh(project(), ["run", "list"])
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()

    def test_a_first_try_success_sleeps_and_logs_nothing(self):
        with self.fake_gh([(0, "[]", "")]) as (fake, sleep, err):
            self.assertEqual(nx.gh(project(), ["run", "list"]), "[]")
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
