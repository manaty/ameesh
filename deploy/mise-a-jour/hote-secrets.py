#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Root-owned stdin dispatcher of the enrolled secret installer (L232c).

Reached only by the `ameesh-secrets` forced command, through sudo, with no
argument. The frame on stdin goes to `coffre-release.helper_main`, which reads
the root-private `/etc/ameesh-secrets/helper.json` and admits only the
destinations it lists. No caller-selected path, command, account or document
is accepted, and the value is never echoed.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys

RUNTIME = Path('/opt/ameesh-release/runtime/bin/python')
# Below the transport's own bound: the caller sees a closed channel, not a
# half-written receipt.
SECONDS = 30


def module(name, label):
    spec = importlib.util.spec_from_file_location(label, Path(__file__).with_name(name+'.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def execute(stdin, stdout, stderr):
    # Same root runtime and trust rule as L232b's restart dispatcher; the
    # restart authority itself is not loaded.
    host = module('hote-release', 'secrets_host_release')
    if (os.geteuid() != 0 or Path(sys.executable) != RUNTIME or not sys.flags.isolated
            or not sys.flags.dont_write_bytecode):
        raise host.HostError('independent root runtime required')
    host.trusted(RUNTIME)
    host.trusted(Path(__file__))
    coffre_path = Path(__file__).with_name('coffre-release.py')
    host.trusted(coffre_path)
    coffre = module('coffre-release', 'secrets_coffre')
    os.environ.clear()
    os.environ.update({'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
    return coffre.helper_main(stdin=stdin, stdout=stdout, stderr=stderr)


def main():
    def expired(signum, frame):
        raise TimeoutError('host request deadline exceeded')
    out = sys.stdout.buffer
    try:
        if len(sys.argv) != 1:
            raise ValueError('host arguments refused')
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(SECONDS)
        # helper_main writes one receipt line, or a category on stderr (which
        # the SSH transport discards); never a value.
        code = execute(sys.stdin.buffer, out, sys.stderr)
        signal.alarm(0)
        return code
    except Exception:
        signal.alarm(0)
        out.write(json.dumps({'error': 'host_action_unavailable'}).encode() + b'\n')
        out.flush()
        return 1


if __name__ == '__main__':
    sys.exit(main())
