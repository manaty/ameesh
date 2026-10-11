# SPDX-License-Identifier: AGPL-3.0-only
"""L226b — recognize a turn that failed for an authentication reason.

A failed turn whose error, short result text or stderr tail says the
credential is no longer valid (HTTP 401, `authentication_error`, "please run
/login", gcloud "Reauthentication failed", expired or revoked OAuth token)
must not be relaunched in a loop: the agent goes to `auth_blocked` and waits
for the credential to be healthy again (L226a sentinel) or for a failover to
a healthy account.

Only failed turns are classified, and only short texts: a successful turn, or
a long final answer that merely *talks* about 401 errors, is never an auth
failure.
"""
from __future__ import annotations

import os
import re

#: longest result text still read as a harness error message (a real auth
#: failure is one short line; a long text is the agent's own answer)
MAX_RESULT_CHARS = 600
#: bytes read at the end of the turn's stderr file
STDERR_TAIL_BYTES = 8192

#: (pattern, reason) — the first match gives the reason
PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), reason) for p, reason in (
        (r"authentication_error", "authentication_error"),
        (r"\b401\b[^\n]{0,80}(unauthori[sz]ed|authenticat|invalid|credential|token)"
         r"|(unauthori[sz]ed|authenticat)[^\n]{0,80}\b401\b", "http 401"),
        (r"invalid (x-)?api[ _-]?key", "invalid api key"),
        (r"please run /login|run `?/login`?|/login to (re)?authenticate", "login required"),
        (r"reauthentication failed", "gcloud reauthentication failed"),
        (r"oauth token (has )?(expired|been revoked)|token (has )?(expired|been revoked)"
         r"|invalid_grant", "token expired or revoked"),
        (r"not logged in|login required|credentials? (have |has )?expired",
         "login required"),
    ))


def classify(error: str | None = None, result: str | None = None,
             stderr_tail: str | None = None) -> str | None:
    """The auth reason of a FAILED turn, None if it is not an auth failure.

    `error`: the error the stream reader extracted; `result`: the turn's
    final text (read only if short); `stderr_tail`: the end of its stderr.
    """
    texts = [error or "", stderr_tail or ""]
    if result and len(result) <= MAX_RESULT_CHARS:
        texts.append(result)
    for text in texts:
        if not text:
            continue
        for pattern, reason in PATTERNS:
            if pattern.search(text):
                return reason
    return None


def stderr_tail(path: str | None, limit: int = STDERR_TAIL_BYTES) -> str:
    """The last `limit` bytes of a stderr file, decoded leniently; '' if none."""
    if not path:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
