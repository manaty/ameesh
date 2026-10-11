# SPDX-License-Identifier: AGPL-3.0-only
"""L226b — classification of authentication failures in failed turns."""
from __future__ import annotations

import os
import tempfile
import unittest

from ameesh import auth_failure


class ClassifyTest(unittest.TestCase):
    def test_known_auth_messages(self):
        cases = {
            'API Error: 401 {"type":"error","error":{"type":"authentication_error",'
            '"message":"OAuth token has expired."}}': "authentication_error",
            "Invalid API key · Please run /login": "invalid api key",
            "Please run /login to authenticate": "login required",
            "ERROR: (gcloud.auth.print-access-token) Reauthentication failed. "
            "cannot prompt during non-interactive execution.": "gcloud reauthentication failed",
            "unexpected status 401 Unauthorized: Missing bearer": "http 401",
            "OAuth token has expired": "token expired or revoked",
            "invalid_grant: Token has been expired or revoked.": "token expired or revoked",
        }
        for text, reason in cases.items():
            self.assertEqual(auth_failure.classify(error=text), reason, text)
            self.assertEqual(auth_failure.classify(stderr_tail="noise\n" + text), reason, text)

    def test_result_text_read_only_when_short(self):
        self.assertEqual(auth_failure.classify(error="success",
                                               result="Invalid API key · Please run /login"),
                         "invalid api key")
        long_answer = ("I fixed the handler that returned 401 Unauthorized for expired "
                       "tokens. " * 20)
        self.assertGreater(len(long_answer), auth_failure.MAX_RESULT_CHARS)
        self.assertIsNone(auth_failure.classify(error="success", result=long_answer))

    def test_other_failures_are_not_auth(self):
        for text in ("rate_limit_error: 429 Too Many Requests", "overloaded_error",
                     "le harnais a rendu le code 1", "context window exceeded",
                     "bail perdu en cours de tour", "HTTP 4010 widgets", ""):
            self.assertIsNone(auth_failure.classify(error=text, stderr_tail=text), text)

    def test_stderr_tail(self):
        self.assertEqual(auth_failure.stderr_tail(None), "")
        self.assertEqual(auth_failure.stderr_tail("/nonexistent/stderr.log"), "")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stderr.log")
            with open(path, "wb") as fh:
                fh.write(b"x" * 20000 + b"\xff Reauthentication failed.\n")
            tail = auth_failure.stderr_tail(path)
            self.assertLessEqual(len(tail.encode("utf-8")), auth_failure.STDERR_TAIL_BYTES + 8)
            self.assertEqual(auth_failure.classify(stderr_tail=tail),
                             "gcloud reauthentication failed")


if __name__ == "__main__":
    unittest.main()
