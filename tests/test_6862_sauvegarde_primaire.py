# SPDX-License-Identifier: AGPL-3.0-only
"""#6862 (EP8.5) : la sauvegarde quotidienne lit le primaire de la grappe.

Le DSN de `config.json` devient multi-hôtes ; sans `target_session_attrs`,
libpq peut s'arrêter sur un secours. `sauvegarder-base.sh` passe `--primary`
au helper, qui pose `target_session_attrs=read-write` avant pg_dump.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]


def _helper():
    spec = importlib.util.spec_from_file_location(
        "sauvegarder_prive_6862", REPO / "deploy/mise-a-jour/sauvegarder-prive.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrimaryDumpTest(unittest.TestCase):
    def setUp(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("psycopg absent")
        self.helper = _helper()
        self.tmp = tempfile.TemporaryDirectory(prefix="t6862-dump-")
        self.addCleanup(self.tmp.cleanup)
        self.config = pathlib.Path(self.tmp.name) / "config.json"
        self.config.write_text(json.dumps({"dsn": (
            "host=10.78.0.11,10.78.0.12,10.78.0.10 dbname=ameesh user=ameesh "
            "sslmode=require")}))

    def parameters(self, *argv):
        with mock.patch.object(self.helper, "dump_parameters") as dumped, \
                mock.patch("sys.argv", ["helper", "dump", "--config", str(self.config),
                                        "--output", "/nonexistent/x.dump", *argv]):
            self.assertEqual(self.helper.main(), 0)
        return dumped.call_args.args[0]

    def test_primary_flag_requires_a_read_write_server(self):
        parameters = self.parameters("--primary")
        self.assertEqual(parameters["target_session_attrs"], "read-write")
        self.assertEqual(parameters["host"], "10.78.0.11,10.78.0.12,10.78.0.10")

    def test_without_the_flag_the_dsn_is_unchanged(self):
        self.assertNotIn("target_session_attrs", self.parameters())

    def test_the_server_script_asks_for_the_primary(self):
        script = (REPO / "deploy/serveur/sauvegarder-base.sh").read_text()
        call = script[script.index('"$helper" dump'):]
        call = call[:call.index("\n\n")] if "\n\n" in call else call
        self.assertIn("--primary", call)


if __name__ == "__main__":
    unittest.main()
