# SPDX-License-Identifier: AGPL-3.0-only
"""Run an application entrypoint without retaining its process-wide guard."""
import subprocess
from unittest import mock


def run(entrypoint, *args, **kwargs):
    # Exercise the real installation during the call, including its children.
    # Restore even when argparse exits or application startup raises.
    with mock.patch.object(subprocess.Popen, "__init__", subprocess.Popen.__init__):
        return entrypoint(*args, **kwargs)
