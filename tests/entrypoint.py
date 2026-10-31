# SPDX-License-Identifier: AGPL-3.0-only
"""Run an application entrypoint without retaining its process-wide guard."""
import subprocess
from unittest import mock


def run(entrypoint, *args, **kwargs):
    # Exercise the real child filter, but do not harden the shared test
    # interpreter: its later tests must still inspect their own environment.
    # Real kernel hardening is covered by dedicated subprocess witnesses.
    # Restore even when argparse exits or application startup raises.
    with mock.patch.object(subprocess.Popen, "__init__", subprocess.Popen.__init__), \
            mock.patch("ameesh.platform.process.become_undumpable", return_value=True):
        return entrypoint(*args, **kwargs)
