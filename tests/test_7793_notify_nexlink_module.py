# SPDX-License-Identifier: AGPL-3.0-only
"""#7793 (#7637 tranche 3b): the `nexlink` alert channel is a module.

The core keeps no import of the channel envelope or its transport: a channel
kind is declared by a built-in module (`NOTIFY_CHANNELS`) and read through
``registry.notify_channel``. The bare profile has no built-in module, hence no
`nexlink` channel and no crypto/envelope import.
"""
from __future__ import annotations

import ast
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from tests import support  # noqa: F401 - isolate host config before product imports
from ameesh import notify
from ameesh.modules import registry

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "ameesh"


@contextlib.contextmanager
def no_entry_points():
    with mock.patch("ameesh.modules._entry_points", return_value=()), \
            mock.patch("ameesh.modules.registry._entry_points", return_value=()):
        yield


class DiscoveryTest(unittest.TestCase):
    def test_the_channel_is_a_module_capability(self):
        with no_entry_points():
            self.assertEqual(registry.notify_channel_kinds(), ("nexlink",))
        module = registry.notify_channel("nexlink")
        self.assertEqual(module.__name__, "ameesh.modules.nexlink.notify")
        self.assertEqual(module.KINDS, ("nexlink",))
        self.assertIn("nexlink", notify.channel_kinds())
        self.assertIsNone(registry.notify_channel("slack"))

    def test_the_implementation_is_loaded_only_when_asked(self):
        script = (
            "import sys\n"
            "from ameesh.modules import registry\n"
            "assert 'ameesh.modules.nexlink.notify' not in sys.modules\n"
            "assert registry.notify_channel('nexlink').__name__ == "
            "'ameesh.modules.nexlink.notify'\n"
            "assert 'ameesh.modules.nexlink.notify' in sys.modules\n")
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        done = subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)


class BareProfileTest(unittest.TestCase):
    def test_no_module_means_no_nexlink_channel(self):
        with mock.patch.dict(os.environ, {"AMEESH_MODULES": "bare"}), no_entry_points():
            self.assertEqual(registry.notify_channel_kinds(), ())
            self.assertIsNone(registry.notify_channel("nexlink"))
            self.assertEqual(notify.channel_kinds(),
                             ("desktop", "ntfy", "slack", "app"))
            with self.assertRaisesRegex(notify.NotifyConfigError, "unknown type"):
                notify.parse_config({
                    "routes": {"human:owner": ["nx"]},
                    "channels": {"nx": {"type": "nexlink",
                                        "url": "https://api.example",
                                        "token_env": "S"}}})

    def test_the_bare_profile_imports_the_notifier_without_the_module(self):
        script = (
            "import sys\n"
            "import ameesh.notify as notify\n"
            "assert notify.channel_kinds() == ('desktop', 'ntfy', 'slack', 'app')\n"
            "assert 'ameesh.modules.nexlink.notify' not in sys.modules\n"
            "assert 'ameesh.modules.nexlink.alerts' not in sys.modules\n")
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
                   AMEESH_MODULES="bare")
        done = subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)


class CoreImportTest(unittest.TestCase):
    def test_the_core_never_imports_the_channel(self):
        tree = ast.parse((SOURCE / "notify.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertNotIn("nexlink", module)
                for alias in node.names:
                    self.assertNotIn("nexlink", alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("nexlink", alias.name)

    def test_the_channel_helpers_left_the_core(self):
        self.assertFalse((SOURCE / "nexlink_alerts.py").exists())
        self.assertTrue(
            (SOURCE / "modules" / "nexlink" / "alerts.py").is_file())
        self.assertTrue(
            (SOURCE / "modules" / "nexlink" / "notify.py").is_file())


class IdentityTest(unittest.TestCase):
    def test_the_generic_alert_id_is_stable(self):
        import uuid
        first = notify.alert_id_for("x")
        self.assertEqual(first, notify.alert_id_for("x"))
        self.assertEqual(str(uuid.UUID(first)), first)
        self.assertNotEqual(first, notify.alert_id_for("y"))


if __name__ == "__main__":
    unittest.main()
