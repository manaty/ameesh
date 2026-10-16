# SPDX-License-Identifier: AGPL-3.0-only
"""Offline checks of the VM launch boundary; no Podman, SQL or VM required."""
import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import unittest

from ameesh.mediated_executor.vm_recipe import command


EXAMPLE = Path(__file__).resolve().parents[1] / "deploy/guest/mediated-recipe.example.json"


class VMRecipe(unittest.TestCase):
    def setUp(self):
        self.recipe = json.loads(EXAMPLE.read_text())

    def test_boundary(self):
        argv = command(self.recipe)
        for flag in ("--user=10001:10001", "--userns=keep-id:uid=10001,gid=10001",
                     "--stop-timeout=100", "--read-only", "--cap-drop=ALL",
                     "--pull=never", "--http-proxy=false", "--memory=3072m",
                     "--memory-swap=3072m", "--cpus=2"):
            self.assertIn(flag, argv)
        self.assertIn('--env=AMEESH_HOST_GATE_LENDING=1', argv)
        self.assertIn("/run/ameesh-gate/state:/run/ameesh-gate/state:ro", argv)
        self.assertIn("/run/ameesh-gate/ack:/run/ameesh-gate/ack:rw", argv)
        self.assertIn("/run/ameesh-enroll:/run/ameesh-enroll:ro", argv)
        self.assertEqual(argv[-2:], [self.recipe["image"], "run"])
        self.assertFalse(any("AMEESH_DSN" in arg or "TOKEN" in arg for arg in argv))

    def test_refuses_secrets_and_overrides(self):
        for field in ("code", "AMEESH_DSN", "env", "args", "HTTPS_PROXY"):
            recipe = dict(self.recipe, **{field: "secret"})
            with self.subTest(field=field), self.assertRaises(ValueError):
                command(recipe)

    def test_refuses_wrong_origin(self):
        for url in ("http://mesh.example.org", "https://u:p@mesh.example.org",
                    "https://mesh.example.org:444", "https://mesh.example.org/",
                    "https://mesh.example.org?x=1", "https://mesh.example.org#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                command(dict(self.recipe, server_url=url))

    def test_requires_immutable_executor_image(self):
        for image in ("registry.example.org/ameesh-executor:latest",
                      "registry.example.org/other@sha256:" + "0" * 64):
            with self.subTest(image=image), self.assertRaises(ValueError):
                command(dict(self.recipe, image=image))

    def test_rejects_mount_aliases_and_option_injection(self):
        for path in ("/", "relative", "/srv/../etc", "/srv/volume:ro", "/srv/volume,ro",
                     "/srv/volume\n", "/run/ameesh-gate/state", "/run/ameesh-gate"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                command(dict(self.recipe, volume=path))

    def test_caps_are_required_and_bounded(self):
        for key, value in (("cpus", 0), ("cpus", float("nan")), ("cpus", True),
                           ("memory_mb", -1), ("tmp_mb", "256"), ("tmp_mb", 3072)):
            recipe = copy.deepcopy(self.recipe)
            recipe[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                command(recipe)

    def test_render_only_without_runtime_or_mounted_directories(self):
        proc = subprocess.run([sys.executable, "-m", "ameesh.mediated_executor.vm_recipe",
                               str(EXAMPLE)], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(shlex.split(proc.stdout), command(self.recipe))


if __name__ == "__main__":
    unittest.main()
