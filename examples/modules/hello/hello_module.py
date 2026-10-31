# SPDX-License-Identifier: AGPL-3.0-only
"""A selectable lifecycle descriptor with a read-only greeting command."""
import argparse

NAME = "hello"
TYPES = ("hello-task",)
CAPABILITIES = ("lifecycle", "cli")


def descriptor(options=None):
    options = dict(options or {})
    if options:
        raise ValueError("unknown hello module option(s): %s" % ", ".join(sorted(options)))
    return {"types": list(TYPES), "schemas": {}, "rules": []}


def main(db, argv, *, actor):
    parser = argparse.ArgumentParser(prog="ameesh module hello")
    parser.add_argument("--name", default="world")
    args = parser.parse_args(argv)
    print("Hello, %s!" % args.name)
    return 0
