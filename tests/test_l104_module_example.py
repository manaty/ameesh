# SPDX-License-Identifier: AGPL-3.0-only
"""The shipped hello package can be discovered as a real external module."""
import configparser
import contextlib
from importlib import metadata
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests import support
from ameesh.modules import registry

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "modules" / "hello"


class HelloModuleTest(unittest.TestCase):
    def test_package_discovery_and_command(self):
        # Read the actual packaging metadata, not a duplicate entry-point stub.
        # This example uses only quoted scalar values; read that TOML subset
        # without requiring tomllib (ameesh also supports Python 3.9/3.10).
        package = configparser.ConfigParser()
        package.read(EXAMPLE / "pyproject.toml")
        project = {key: json.loads(value) for key, value in package["project"].items()}
        entries = {key: json.loads(value) for key, value in
                   package['project.entry-points."ameesh.modules"'].items()}
        with tempfile.TemporaryDirectory() as directory:
            info = Path(directory) / "ameesh_hello_module-0.1.0.dist-info"
            info.mkdir()
            (info / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: %s\nVersion: %s\n" %
                (project["name"], project["version"]))
            (info / "entry_points.txt").write_text(
                "[ameesh.modules]\n" + "\n".join(
                    "%s = %s" % entry for entry in entries.items()) + "\n")
            points = [point for dist in metadata.distributions(path=[directory])
                      for point in dist.entry_points]
            with mock.patch.object(sys, "path", [str(EXAMPLE)] + sys.path), \
                    mock.patch("ameesh.modules.metadata.entry_points",
                               return_value=metadata.EntryPoints(points)):
                implementations, rows, refused = registry._discover(builtin=False)
                self.assertEqual(refused, ())
                self.assertEqual(len(rows), 1)
                hello = rows[0]
                self.assertEqual((hello.name, hello.source, hello.version),
                                 ("hello", "external", "0.1.0"))
                self.assertEqual(hello.storage, ("hello-task",))
                self.assertEqual(hello.capabilities, ("lifecycle", "cli"))
                self.assertEqual(hello.cli, "ameesh module hello")
                module = implementations["hello"]
                with self.assertRaisesRegex(ValueError, "unknown hello"):
                    module.descriptor({"typo": True})
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(module.main(None, ["--name", "Alice"], actor=""), 0)
                self.assertEqual(output.getvalue(), "Hello, Alice!\n")
                with contextlib.redirect_stdout(io.StringIO()), \
                        self.assertRaises(SystemExit) as stopped:
                    module.main(None, ["--help"], actor="")
                self.assertEqual(stopped.exception.code, 0)
            sys.modules.pop("hello_module", None)
