#!/usr/bin/env python3
"""Consumer side of `ameesh-responder/1` (#7866).

Reads the JSON document of `ameesh responder --json` on stdin and prints the
selected responder (or the wake status).  Exits 0 only when the document is a
valid routing: a `human:<id>` responder, or a requested phone wake.  A refusal,
an invalid document or a routing with nobody available exits 1.

This fixture is deliberately dependency-free: it is the shape an external
consumer (IMA, an avatar) implements against
`docs/api/incident-responder-v1.md`.
"""
from __future__ import annotations

import json
import sys

SCHEMA = "ameesh-responder/1"
SEVERITIES = ("P1", "P2", "P3", "P4", "security", "data-loss", "outage")
SIGNALS = ("attach", "interaction", "presence", "timezone")


def consume(document):
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError("not an %s document" % SCHEMA)
    if document.get("ok") is not True:
        error = document.get("error") or {}
        raise ValueError("refused: %s" % (error.get("code") or "no responder"))
    application = document.get("application") or {}
    if not isinstance(application.get("id"), str) \
            or not isinstance(application.get("title"), str):
        raise ValueError("application {id, title} missing")
    severity = document.get("severity")
    if severity not in SEVERITIES:
        raise ValueError("unknown severity %r" % (severity,))
    signals = document.get("signals")
    if not isinstance(signals, list) or any(s not in SIGNALS for s in signals):
        raise ValueError("signals must be a list of kinds")
    responder = document.get("responder")
    wake = document.get("wake") or {}
    if responder is not None:
        member = responder.get("member")
        if not isinstance(member, str) or not member.startswith("human:"):
            raise ValueError("responder.member must be human:<id>")
        return "responder %s for %s %s via %s" % (
            member, application["id"], severity, document.get("reason"))
    if wake.get("status") == "requested":
        return "wake requested for %s %s" % (application["id"], severity)
    raise ValueError("nobody available and no wake requested")


def main() -> int:
    try:
        document = json.load(sys.stdin)
        print(consume(document))
    except (ValueError, json.JSONDecodeError) as exc:
        print("responder-consumer: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
