#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""In-Job witness of the TPK #2202 sandbox path (qualification only).

This file is *not* an admitted reviewer image and is never imported by ameesh.
The qualification passes it to the Job with ``python -c <script> <mode>
<relay|norelay>``; the image is a pinned, public, non-root
``distroless/python3-debian12`` digest.
It runs with no ameesh source, no database handle and no provider key: the two
files it reads are the turn Secret mounted at ``/run/ameesh/turn``.

It writes exactly one canonical ``ameesh-sandbox-result/1`` envelope on stdout
(the supervisor rejects any other byte) and the matching digest in the
termination message (4 KiB bound). Diagnostics never go to stdout: a failed
Job exits non-zero and is closed as ``failed`` by S1, never as a result.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

TURN_DIR = "/run/ameesh/turn"
RESULT_SCHEMA = "ameesh-sandbox-result/1"
JOB_SCHEMA = "ameesh-sandbox-job/1"


def canonical(value):
    """JCS subset used by the qualification envelope.

    Every number of the envelope is an integer and every map key is a string,
    so sorting keys and serializing compactly is exactly ``jcs.canonicalize``
    (no float formatting, no Unicode surprises: values are ASCII or UTF-8
    strings emitted with ``ensure_ascii=False``).
    """
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("non-string map key")
            parts.append(json.dumps(key, ensure_ascii=False) + ":" + canonical(value[key]))
        return "{" + ",".join(parts) + "}"
    if isinstance(value, list):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    raise TypeError("non-canonical value: %r" % (type(value).__name__,))


def read_input():
    with open(os.path.join(TURN_DIR, "prompt"), encoding="utf-8") as stream:
        document = json.load(stream)
    if not isinstance(document, dict) or document.get("schema") != JOB_SCHEMA:
        raise SystemExit("unexpected Job input")
    return document


def relay_call(document):
    """One model call through the turn-scoped relay token.

    The token is read from the mounted Secret, never printed and never placed
    in the Job argv. A refusal, a timeout or a missing answer fails the Job:
    the qualification never turns a broken model path into a green result.
    """
    token_path = os.path.join(TURN_DIR, "relay_token")
    with open(token_path, encoding="utf-8") as stream:
        token = stream.read().strip()
    if not token.startswith("amsb1."):
        raise SystemExit("relay credential missing")
    base = str(document.get("relay_url") or "").rstrip("/")
    if not base:
        raise SystemExit("relay URL missing")
    body = json.dumps({
        "model": "deepseek-flash",
        "max_tokens": 64,
        "stream": False,
        "messages": [{"role": "user", "content": "sandbox-kind qualification"}],
    }).encode()
    request = urllib.request.Request(
        base + "/api/exec/v1/llm/deepseek/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": token},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SystemExit("relay call failed: %s" % type(exc).__name__)
    text = "".join(block.get("text", "") for block in payload.get("content", [])
                   if isinstance(block, dict))
    if "RELAIS-OK" not in text:
        raise SystemExit("relay answer unexpected")


def result(document, *, model_calls):
    scope = document["scope"]
    return {
        "schema": RESULT_SCHEMA,
        "turn_id": document["turn_id"],
        "epoch": document["epoch"],
        "scope": scope,
        "scope_digest": document["scope_digest"],
        "result_id": "kind-" + str(document["turn_id"]),
        "outcome": "accepted",
        "verdict": "green",
        "findings": [{"severity": "N", "code": "kind-witness",
                      "summary": "qualification witness, no review content"}],
        "evidence": [{"kind": "check", "name": "sandbox-kind",
                      "conclusion": "success"}],
        "usage": {"turns": 1, "tool_calls": 1, "seconds": 1,
                  "model_calls": model_calls},
        "cost": {"usd": 0},
        "sandbox_session": None,
        "image_digest": document["image_digest"],
        "source": document["source"],
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }


def main(argv):
    mode = argv[1] if len(argv) > 1 else "complete"
    relay = (argv[2] if len(argv) > 2 else "relay") == "relay"
    if mode == "slow":
        # Cancellation case: long enough for the supervisor to bind, fence and
        # delete the Job before any result exists.
        time.sleep(600)
    elif mode == "delay":
        # Supervisor-restart case: the Job keeps running while the supervisor
        # is replaced, then completes under the same turn/epoch.
        time.sleep(8)
    document = read_input()
    if relay:
        relay_call(document)
    envelope = result(document, model_calls=1 if relay else 0)
    raw = canonical(envelope).encode("utf-8")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    with open("/dev/termination-log", "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"result_sha256": digest}, separators=(",", ":")))
    sys.stdout.write(raw.decode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
