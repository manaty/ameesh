# SPDX-License-Identifier: AGPL-3.0-only
"""#4488 : `harness_bin` n'est lancé que s'il est déclaré sur l'hôte.

#4293 fait lire `agent_registry.harness_bin` par l'exécuteur : un chemin
d'exécutable écrit en base par n'importe quelle identité (`ameesh set`) était
lancé tel quel, avec les identifiants du compte. La garde de lecture n'accepte
plus qu'un binaire DÉCLARÉ localement : `harness_bins` de la configuration,
`AMEESH_<HARNAIS>_BIN`, ou la carte des binaires installés de l'hôte
(`harness-adoption.json`, `AMEESH_HARNESS_ADOPTION_BINS`).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import adapters, harness_adoption, harnesses
from ameesh import runner as runner_mod
from ameesh.adapters import HarnessMissing
from ameesh.runner import AgentWorker


def _script(path: str, body: str = "#!/bin/sh\nexit 0\n") -> str:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class HarnessBinDeclarationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-4488-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.host = "h1"
        self.binary = _script(os.path.join(self.tmp, "dsh"))
        self.evil = _script(os.path.join(self.tmp, "evil"))
        self.map_path = os.path.join(self.tmp, "harness-adoption.json")
        self.env = {"PATH": self.tmp,
                    "AMEESH_HARNESS_ADOPTION_BINS": self.map_path}

    def _write_map(self, bins):
        with open(self.map_path, "w", encoding="utf-8") as fh:
            json.dump({"bins": bins}, fh)

    def _declared(self, host=None):
        with mock.patch.dict(os.environ, self.env, clear=False):
            return harness_adoption.declared_binaries(
                "deepseek", host or self.host, env=self.env)

    def test_config_harness_bins_declares_a_binary(self):
        with mock.patch.dict(os.environ, self.env, clear=False):
            adapters.require_declared_harness_bin(
                "deepseek", self.binary, host=self.host,
                bins={"deepseek": self.binary})
            with self.assertRaises(HarnessMissing) as ctx:
                adapters.require_declared_harness_bin(
                    "deepseek", self.evil, host=self.host,
                    bins={"deepseek": self.binary})
        self.assertIn("not a binary declared", str(ctx.exception))

    def test_harness_env_declares_a_binary(self):
        with mock.patch.dict(os.environ,
                             dict(self.env, AMEESH_DSH_BIN=self.binary),
                             clear=False):
            adapters.require_declared_harness_bin(
                "deepseek", self.binary, host=self.host, bins={})
            with self.assertRaises(HarnessMissing):
                adapters.require_declared_harness_bin(
                    "deepseek", self.evil, host=self.host, bins={})

    def test_local_installed_map_declares_a_binary_for_this_host_only(self):
        self._write_map({"deepseek": {"0.2.1-rc.4": {
            self.host: self.binary, "h2": self.evil}}})
        self.assertEqual(self._declared(), {os.path.realpath(self.binary)})
        self.assertEqual(self._declared("h2"), {os.path.realpath(self.evil)})
        with mock.patch.dict(os.environ, self.env, clear=False):
            adapters.require_declared_harness_bin(
                "deepseek", self.binary, host=self.host,
                extra=self._declared())
            with self.assertRaises(HarnessMissing):
                # déclaré pour h2, jamais pour h1
                adapters.require_declared_harness_bin(
                    "deepseek", self.evil, host=self.host,
                    extra=self._declared())

    def test_any_version_of_the_local_map_declares_the_path(self):
        self._write_map({"deepseek": {
            "0.2.0": {self.host: "/opt/old/dsh"},
            "0.2.1-rc.4": {self.host: self.binary}}})
        self.assertEqual(self._declared(),
                         {os.path.realpath("/opt/old/dsh"),
                          os.path.realpath(self.binary)})

    def test_a_symlink_to_a_declared_binary_is_accepted(self):
        link = os.path.join(self.tmp, "dsh-link")
        os.symlink(self.binary, link)
        with mock.patch.dict(os.environ, self.env, clear=False):
            adapters.require_declared_harness_bin(
                "deepseek", link, host=self.host,
                bins={"deepseek": self.binary})

    def test_malformed_map_fails_closed(self):
        with open(self.map_path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with mock.patch.dict(os.environ, self.env, clear=False):
            with self.assertRaises(harness_adoption.VeilleError):
                harness_adoption.declared_binaries(
                    "deepseek", self.host, env=self.env)
            with self.assertRaises(HarnessMissing):
                runner_mod.declared_adoption_binaries("deepseek", self.host)

    def test_unknown_harness_is_left_to_adapter_for(self):
        adapters.require_declared_harness_bin(
            "inconnu", self.evil, host=self.host, bins={})
        with self.assertRaises(HarnessMissing) as ctx:
            adapters.adapter_for("inconnu", self.evil)
        self.assertIn("unknown harness", str(ctx.exception))

    def test_agent_adapter_applies_the_guard(self):
        with mock.patch.dict(os.environ, self.env, clear=False), \
                mock.patch.dict(adapters._HARNESS_BINS, {"deepseek": self.binary}):
            self.assertEqual(
                runner_mod.agent_adapter("deepseek", self.binary, self.host).binary,
                self.binary)
            with self.assertRaises(HarnessMissing) as ctx:
                runner_mod.agent_adapter("deepseek", self.evil, self.host)
        self.assertIn("not a binary declared", str(ctx.exception))

    def test_worker_adapter_refuses_an_undeclared_pin(self):
        worker = AgentWorker.__new__(AgentWorker)
        threading.Thread.__init__(worker, name="canary", daemon=True)
        worker.cfg = SimpleNamespace(host=self.host)
        worker.descriptor_seen = None
        worker.resolution_seen = ""
        worker.turn_harness = lambda: "deepseek"
        worker.agent = {"name": "canary", "harness": "deepseek",
                        "harness_bin": self.binary}
        with mock.patch.dict(os.environ, self.env, clear=False), \
                mock.patch.dict(adapters._HARNESS_BINS, {"deepseek": self.binary}):
            self.assertEqual(worker._adapter().binary, self.binary)
        worker.agent = {"name": "canary", "harness": "deepseek",
                        "harness_bin": self.evil}
        with mock.patch.dict(os.environ, self.env, clear=False), \
                mock.patch.dict(adapters._HARNESS_BINS, {"deepseek": self.binary}):
            with self.assertRaises(HarnessMissing):
                worker._adapter()

    def test_a_fallback_harness_does_not_use_the_pin(self):
        """Le binaire épinglé est celui du harnais de l'agent : un tour de
        repli (L242b) reprend la résolution de l'hôte, sans la garde du pin."""
        worker = AgentWorker.__new__(AgentWorker)
        threading.Thread.__init__(worker, name="canary", daemon=True)
        worker.cfg = SimpleNamespace(host=self.host)
        worker.descriptor_seen = None
        worker.resolution_seen = ""
        worker.turn_harness = lambda: "codex"
        worker.agent = {"name": "canary", "harness": "deepseek",
                        "harness_bin": self.evil}

        class _Adapter:
            descriptor = SimpleNamespace(path="x", sha256="", id="codex", source="t")
            resolution = None

        adapter = _Adapter()
        with mock.patch.object(runner_mod, "agent_adapter",
                               return_value=adapter) as called:
            self.assertIs(worker._adapter(), adapter)
        self.assertEqual(called.call_args.args[0], "codex")
        self.assertIsNone(called.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
