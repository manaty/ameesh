# SPDX-License-Identifier: AGPL-3.0-only
"""`ameesh --version`, `ameesh version` et la ligne de version de `ameesh
doctor` : la version du paquet, lue par importlib.metadata (repli sur le
pyproject.toml de l'arbre source)."""
from __future__ import annotations

import contextlib
import io
import os
import re
import unittest
from importlib import metadata
from unittest import mock

import ameesh
from ameesh import cli, config as config_mod, db as db_mod, main as main_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pyproject_version() -> str:
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        return re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.M).group(1)


def _output(argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main_mod.main(argv)
    return code, out.getvalue()


class VersionTest(unittest.TestCase):
    def test_version_read_by_importlib_metadata(self):
        with mock.patch.object(metadata, "version", return_value="9.8.7") as read:
            self.assertEqual(ameesh.version(), "9.8.7")
        read.assert_called_once_with("ameesh")

    def test_falls_back_to_pyproject_when_not_installed(self):
        with mock.patch.object(metadata, "version",
                               side_effect=metadata.PackageNotFoundError("ameesh")):
            self.assertEqual(ameesh.version(), _pyproject_version())

    def test_version_is_semver(self):
        self.assertRegex(ameesh.version(), r"^\d+\.\d+\.\d+")

    def test_version_command_and_flag(self):
        for argv in (["--version"], ["version"]):
            code, text = _output(argv)
            self.assertEqual(code, 0, argv)
            self.assertEqual(text, "ameesh %s\n" % ameesh.version(), argv)

    def test_doctor_prints_version_first(self):
        cfg = config_mod.load({"AMEESH_DSN": "postgresql://personne@127.0.0.1:1/rien",
                               "AMEESH_CONFIG": os.path.join(ROOT, ".config-absente.json")})
        out = io.StringIO()
        with mock.patch.object(db_mod, "connect", side_effect=db_mod.Unavailable("test")), \
                contextlib.redirect_stdout(out):
            code = cli.cmd_doctor(cfg, notify_test=False)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue().splitlines()[0],
                         "version    : ameesh %s" % ameesh.version())


if __name__ == "__main__":
    unittest.main()
