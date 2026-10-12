# SPDX-License-Identifier: AGPL-3.0-only
"""Offline placement policy and optional real dsh provider regression (#490)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import support  # isolate the host's agent environment
from ameesh import adapters, deepseek_cgroup
from ameesh.runner import AgentWorker


NODE = shutil.which("node")
PROVIDER = os.environ.get("DSH_TEST_SUBPROCESS_LOCAL", "")


class EnvironmentTest(unittest.TestCase):
    def test_node_options_are_idempotent(self):
        options = deepseek_cgroup.node_options("--max-old-space-size=2048")
        self.assertEqual(deepseek_cgroup.node_options(options), options)

    def test_linux_deepseek_preserves_existing_node_options(self):
        with mock.patch.dict(os.environ, {"NODE_OPTIONS": "--max-old-space-size=2048"}), \
                mock.patch("ameesh.platform.is_linux", return_value=True):
            adapter = adapters.adapter_for("deepseek", resolve=False)
            env = adapter.env()
            self.assertEqual(env["DSH_PERMISSION_MODE"], "danger-full-access")
            self.assertTrue(env["NODE_OPTIONS"].startswith("--max-old-space-size=2048 "))
            self.assertIn(str(deepseek_cgroup.PRELOAD), env["NODE_OPTIONS"])

    def test_other_harnesses_and_platforms_keep_environment(self):
        for key in ("claude", "codex"):
            self.assertNotIn("NODE_OPTIONS", adapters.adapter_for(key, resolve=False).env())
        with mock.patch("ameesh.platform.is_linux", return_value=False):
            self.assertNotIn("NODE_OPTIONS", adapters.adapter_for("deepseek", resolve=False).env())

    def test_preload_failure_uses_existing_host_alert_classification(self):
        with tempfile.NamedTemporaryFile(mode="w") as stderr:
            stderr.write("[ameesh] WARNING: DeepSeek cgroup protection unavailable: unreviewed provider\n")
            stderr.flush()
            reason = AgentWorker._echec_d_hote(127, 0, None, 0.1, stderr.name)
        self.assertIn("DeepSeek cgroup protection unavailable", reason)


@unittest.skipUnless(NODE, "Node is required for the preload contract")
class PreloadTest(unittest.TestCase):
    def run_node(self, code, env=None):
        result = subprocess.run([NODE, "--input-type=module", "-e", code],
                                capture_output=True, text=True, timeout=15, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_probe_is_denied_before_execution_and_esm_bindings_are_updated(self):
        code = """
import {createRequire} from 'node:module';
import {spawnSync, spawn} from 'node:child_process';
import assert from 'node:assert/strict';
const require=createRequire(import.meta.url);
const policy=require(PRELOAD);
policy.installPolicy();
const probe=['--user','--scope','--unit=dsh-subprocess-probe-42-abcdef','--','sh','-c','exit 0'];
const denied=spawnSync('/does/not/exist/systemd-run',probe);
assert.equal(denied.error.code,'ENOTSUP');
assert.equal(denied.pid,0);
assert.equal(denied.status,null);
assert.throws(()=>spawn('systemd-run',['--user','--scope','--unit=dsh-subprocess-42-abcdef','--','true']), /requires review/);
const ordinary=spawnSync(process.execPath,['-e','console.log("ordinary")'],{encoding:'utf8'});
assert.equal(ordinary.status,0);
assert.equal(ordinary.stdout.trim(),'ordinary');
assert.equal(policy.isDshScope('systemd-run',['--user','--scope','--unit=other-42-abcdef']),false);
assert.equal(policy.isDshScope('systemd-run',['--scope','--unit=dsh-subprocess-42-abcdef']),false);
""".replace("PRELOAD", json.dumps(str(deepseek_cgroup.PRELOAD)))
        self.run_node(code)

    def test_unreviewed_provider_and_missing_module_are_loud_before_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"name":"@deepseek-ai/dsh"}')
            entry = root / "entry.cjs"
            entry.write_text('console.log("COMMAND RAN");')
            env = dict(os.environ, NODE_OPTIONS=deepseek_cgroup.node_options())
            for module in (None, "unexpected", "0.1.5-rc.2"):
                if module:
                    provider = root / "node_modules/@deepseek-ai/dsh-subprocess-local"
                    provider.mkdir(parents=True, exist_ok=True)
                    (provider / "package.json").write_text(json.dumps({
                        "name": "@deepseek-ai/dsh-subprocess-local", "version": module,
                        "main": "index.js"}))
                    (provider / "index.js").write_text("// changed or unreviewed probe\n")
                result = subprocess.run([NODE, str(entry)], env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 127, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("WARNING: DeepSeek cgroup protection unavailable", result.stderr)

    def test_preload_path_with_spaces_and_quotes_still_activates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'space and "quote"'
            root.mkdir()
            preload = root / "preload.cjs"
            shutil.copy(deepseek_cgroup.PRELOAD, preload)
            entry = root / "entry.cjs"
            entry.write_text('console.log("COMMAND RAN");')
            with mock.patch.object(deepseek_cgroup, "PRELOAD", preload):
                env = dict(os.environ, NODE_OPTIONS=deepseek_cgroup.node_options())
            result = subprocess.run([NODE, str(entry)], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 127, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertIn("WARNING: DeepSeek cgroup protection unavailable", result.stderr)


class PlacementCheckTest(unittest.TestCase):
    def test_outside_scope_and_reparented_scope_member_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            root = "/ameesh.slice/ameesh-agents.slice/runner.service"
            rows = {10: (1, root), 20: (10, root), 30: (20, root + "/child"),
                    40: (20, "/app.slice/dsh-subprocess-20-abcdef.scope"),
                    50: (1, "/app.slice/dsh-subprocess-20-abcdef.scope"),
                    60: (1, "/app.slice/dsh-subprocess-99-abcdef.scope"),
                    70: (20, root + "-unrelated"), 80: (20, root)}
            for pid, (parent, cgroup) in rows.items():
                entry = proc / str(pid)
                entry.mkdir()
                (entry / "stat").write_text(f"{pid} (name (with) spaces) S {parent} 0 0")
                (entry / "cgroup").write_text(f"0::{cgroup}\n")
            report = deepseek_cgroup.check(10, proc)
            self.assertFalse(report["ok"])
            self.assertEqual([p["pid"] for p in report["outside"]], [40, 50, 70])
            for pid in (40, 50, 70):
                (proc / str(pid) / "cgroup").write_text(f"0::{root}\n")
            self.assertTrue(deepseek_cgroup.check(10, proc)["ok"])

    def test_unknown_or_unreadable_runner_is_not_a_positive_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                deepseek_cgroup.check(42, Path(tmp))
        with self.assertRaises(ValueError):
            deepseek_cgroup.unified_cgroup("1:memory:/legacy\n")


@unittest.skipUnless(NODE and PROVIDER and os.name == "posix", "optional installed dsh provider")
class RealProviderTest(unittest.TestCase):
    def invoke(self, mode, preload):
        with tempfile.TemporaryDirectory(prefix="l490-") as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"name":"@deepseek-ai/dsh"}')
            scope = root / "node_modules/@deepseek-ai"
            scope.mkdir(parents=True)
            (scope / "dsh-subprocess-local").symlink_to(Path(PROVIDER).resolve())
            entry = root / "entry.mjs"
            shutil.copy(Path(__file__).with_name("l490_provider.mjs"), entry)
            env = os.environ.copy()
            env.pop("NODE_OPTIONS", None)
            if preload:
                env["NODE_OPTIONS"] = deepseek_cgroup.node_options("--max-old-space-size=256")
            result = subprocess.run([NODE, str(entry), PROVIDER, mode], env=env,
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout), result.stderr

    def test_without_preload_scope_with_preload_inherited_including_setsid(self):
        baseline, _ = self.invoke("ordinary", False)
        fixed, stderr = self.invoke("ordinary", True)
        self.assertIn("/dsh-subprocess-", baseline["groups"][0])
        self.assertNotEqual(baseline["parent"], baseline["groups"][0])
        self.assertEqual(fixed["groups"], [fixed["parent"]] * 3)
        self.assertIn("weaker process-tree containment", stderr)
        self.assertEqual(fixed["nodeOptions"], "--max-old-space-size=256")

    def test_pty_keeps_cgroup_and_terminates(self):
        fixed, _ = self.invoke("terminal", True)
        self.assertEqual(fixed["groups"], [fixed["parent"]] * 3)

    def test_normal_managed_process_termination_still_works(self):
        fixed, _ = self.invoke("terminate", True)
        self.assertIsNone(fixed["outcome"]["exitCode"])
        self.assertIn(fixed["outcome"]["signal"], ("SIGTERM", "SIGKILL"))
