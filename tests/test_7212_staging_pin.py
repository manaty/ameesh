# SPDX-License-Identifier: AGPL-3.0-only
"""#7212 : l'épingle expected_sha256 suit le venv vivant de l'exécutant.

Le banc rejoue la bascule réelle de `bascule.py` sur des venvs factices : le
changement de `venv-courant` réécrit les entrées de venv de
`staging-executor.json` (chemin et empreinte), laisse les autres entrées
intactes, et `staging_executor.fingerprints` accepte la demande. Témoins :
deux bascules de suite, un retour arrière, une entrée stable `venv-courant`,
un refus qui ne change rien, et la garde toujours active après réépinglage
(une modification sur place reste refusée).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from ameesh import staging_executor as executor

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "bascule_7212", ROOT / "deploy" / "mise-a-jour" / "bascule.py")
bascule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bascule)


def faux_venv(base: Path, version: str, code: str) -> Path:
    """Un venv versionné factice : `.pret`, python et paquet ameesh."""
    path = base / ("venv-%s" % version)
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "python").write_bytes(("interpreter-" + version).encode())
    package = path / "lib" / "python3.12" / "site-packages" / "ameesh"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("CODE = %r\n" % code)
    (path / bascule.MARKER).write_text(json.dumps({"version": version}))
    return path


def package_of(venv: Path) -> Path:
    return venv / "lib" / "python3.12" / "site-packages" / "ameesh"


class PinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pin-7212-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)
        self.venv = self.base / "venv"
        (self.venv / "bin").mkdir(parents=True)
        self.config = self.base / "staging-executor.json"
        self.probe = self.base / "quiescence-staging.sh"
        self.probe.write_text("#!/bin/sh\nexit 0\n")
        self.bascule_env = self.base / "bascule.env"
        self.bascule_env.write_text("")
        self.first = faux_venv(self.base, "aaa", "first")
        os.symlink("venv-aaa", self.base / bascule.CURRENT)
        self.second = faux_venv(self.base, "bbb", "second")
        # Le banc isole l'épingle : l'appartenance des chemins est celle de
        # #5500 (tests dédiés), et le répertoire temporaire vit sous .menage.
        for name in ("path_problem", "tree_problem"):
            patcher = mock.patch.object(executor, name, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def options(self):
        return dict(key="mesh", project="test", role="pilot",
                    candidates=["host-a", "host-b"], repo=str(self.base),
                    bascule_env=str(self.bascule_env), github="o/r",
                    config_path=str(self.config),
                    quiescence_probe=[str(self.probe)])

    def environment(self):
        return {"HOME": str(self.base), "PATH": os.environ.get("PATH", ""),
                "QUIESCENCE_PYTHON": str(self.venv / "bin" / "python")}

    def write_config(self, expected, **extra):
        data = dict(key="mesh", project="test", role="pilot",
                    candidates=["host-a", "host-b"], repo=str(self.base),
                    bascule_env=str(self.bascule_env), github="o/r",
                    quiescence_probe=[str(self.probe)],
                    expected_sha256=expected)
        data.update(extra)
        self.config.write_text(json.dumps(data, indent=2) + "\n")
        self.config.chmod(0o600)
        return self.config.read_bytes()

    def initial_pins(self):
        return {
            str(self.first / "bin" / "python"):
                bascule._pin_digest(self.first / "bin" / "python"),
            str(package_of(self.first)):
                executor._tree_sha256(package_of(self.first)),
            str(self.probe): bascule._pin_digest(self.probe),
        }

    def run_activer(self, version="bbb", precedent="aaa", extra=()):
        argv = ["activer", "--venv", str(self.venv), "--version", version]
        if precedent:
            argv += ["--precedent", precedent]
        argv += list(extra)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                return bascule.main(argv)
            except SystemExit as exc:
                return exc.code

    def run_retour(self, extra=()):
        argv = ["retour", "--venv", str(self.venv),
                "--staging-config", str(self.config)] + list(extra)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                return bascule.main(argv)
            except SystemExit as exc:
                return exc.code

    def pins(self, config=None):
        return json.loads((config or self.config).read_text())["expected_sha256"]

    def fingerprints(self, config=None):
        options = self.options()
        options["expected_sha256"] = self.pins(config)
        return executor.fingerprints(options, self.environment())

    def assert_accepted(self):
        expected = self.pins()
        found, problem = self.fingerprints()
        self.assertIsNone(problem)
        for key, digest in expected.items():
            self.assertEqual(found[os.path.realpath(key)], digest)
        return found

    def test_switch_repins_and_the_request_is_accepted(self):
        before = self.write_config(self.initial_pins())
        self.assertEqual(0, self.run_activer(
            extra=("--staging-config", str(self.config))))
        expected = self.pins()
        self.assertEqual(set(expected), {
            str(self.second / "bin" / "python"),
            str(package_of(self.second)),
            str(self.probe),
        })
        self.assertEqual(expected[str(self.second / "bin" / "python")],
                         bascule._pin_digest(self.second / "bin" / "python"))
        self.assertEqual(expected[str(package_of(self.second))],
                         executor._tree_sha256(package_of(self.second)))
        self.assertEqual(expected[str(self.probe)],
                         bascule._pin_digest(self.probe))
        self.assertNotEqual(before, self.config.read_bytes())
        self.assert_accepted()

    def test_two_switches_keep_the_pin_and_the_guard(self):
        self.write_config(self.initial_pins())
        self.assertEqual(0, self.run_activer(
            extra=("--staging-config", str(self.config))))
        third = faux_venv(self.base, "ccc", "third")
        self.assertEqual(0, self.run_activer(
            "ccc", "bbb", ("--staging-config", str(self.config))))
        expected = self.pins()
        self.assertIn(str(third / "bin" / "python"), expected)
        self.assertIn(str(package_of(third)), expected)
        self.assertNotIn(str(package_of(self.second)), expected)
        self.assert_accepted()
        # La garde reste active : une modification sur place est refusée.
        (package_of(third) / "__init__.py").write_text("CODE = 'tampered'\n")
        _found, problem = self.fingerprints()
        self.assertIn("expected", problem or "")
        (package_of(third) / "__init__.py").write_text("CODE = 'third'\n")
        self.assert_accepted()

    def test_retour_repins_on_the_previous_venv(self):
        self.write_config(self.initial_pins())
        self.assertEqual(0, self.run_activer(
            extra=("--staging-config", str(self.config))))
        self.assertEqual(0, self.run_retour())
        self.assertEqual(os.readlink(self.base / bascule.CURRENT), "venv-aaa")
        expected = self.pins()
        self.assertIn(str(self.first / "bin" / "python"), expected)
        self.assertIn(str(package_of(self.first)), expected)
        self.assertNotIn(str(self.second / "bin" / "python"), expected)
        self.assert_accepted()

    def test_stable_link_pin_keeps_its_path(self):
        stable = {
            str(self.base / bascule.CURRENT / "bin" / "python"):
                bascule._pin_digest(self.first / "bin" / "python"),
            str(self.base / bascule.CURRENT / "lib" / "python3.12"
                / "site-packages" / "ameesh"):
                executor._tree_sha256(package_of(self.first)),
        }
        self.write_config(stable)
        self.assertEqual(0, self.run_activer(
            extra=("--staging-config", str(self.config))))
        expected = self.pins()
        self.assertEqual(set(expected), set(stable))
        self.assert_accepted()

    def test_a_refusal_changes_nothing(self):
        pins = {
            str(self.first / "lib" / "python3.12" / "site-packages" / "ameesh"
                / "gone.py"): "0" * 64,
        }
        before = self.write_config(pins)
        code = self.run_activer(extra=("--staging-config", str(self.config)))
        self.assertEqual(code, 1)
        self.assertEqual(os.readlink(self.base / bascule.CURRENT), "venv-aaa")
        self.assertEqual(self.config.read_bytes(), before)

    def test_config_without_pins_is_left_untouched(self):
        data = {
            "key": "mesh", "project": "test", "role": "pilot",
            "candidates": ["host-a", "host-b"],
        }
        self.config.write_text(json.dumps(data, indent=2) + "\n")
        before = self.config.read_bytes()
        self.assertEqual(0, self.run_activer(
            extra=("--staging-config", str(self.config))))
        self.assertEqual(self.config.read_bytes(), before)

    def test_default_config_is_detected_under_home(self):
        home = self.base / "compte"
        config = home / ".config" / "ameesh" / "staging-executor.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({
            "key": "mesh", "project": "test", "role": "pilot",
            "candidates": ["host-a", "host-b"],
            "expected_sha256": {
                str(self.first / "bin" / "python"):
                    bascule._pin_digest(self.first / "bin" / "python"),
            },
        }, indent=2) + "\n")
        config.chmod(0o600)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(0, self.run_activer())
        self.assertIn(str(self.second / "bin" / "python"),
                      self.pins(config))

    def test_a_foreign_config_is_not_written_implicitly(self):
        home = self.base / "autre"
        config = home / ".config" / "ameesh" / "staging-executor.json"
        config.parent.mkdir(parents=True)
        config.write_text("{}\n")
        config.chmod(0o600)
        before = config.read_bytes()
        with mock.patch.dict(os.environ, {"HOME": str(home)}), \
                mock.patch.object(bascule.os, "geteuid", return_value=0):
            self.assertEqual(0, self.run_activer())
        self.assertEqual(config.read_bytes(), before)

    def test_tree_digest_matches_the_executor(self):
        self.assertEqual(bascule._sha256_tree(package_of(self.first)),
                         executor._tree_sha256(package_of(self.first)))
        (package_of(self.first) / "__pycache__").mkdir()
        (package_of(self.first) / "__pycache__" / "x.pyc").write_bytes(b"cache")
        self.assertEqual(bascule._sha256_tree(package_of(self.first)),
                         executor._tree_sha256(package_of(self.first)))


if __name__ == "__main__":
    unittest.main()
