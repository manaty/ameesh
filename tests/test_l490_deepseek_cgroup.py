# SPDX-License-Identifier: AGPL-3.0-only
"""Offline placement policy and optional real dsh provider regression (#490)."""
from __future__ import annotations

import json
import hashlib
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

    def test_only_provider_bindings_deny_probe_and_other_modules_keep_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "systemd-run"
            binary.write_text('#!/bin/sh\nprintf "foreign-ok\\n"\n')
            binary.chmod(0o755)
            code = """
import {createRequire} from 'node:module';
import {spawnSync as foreignSync} from 'node:child_process';
import assert from 'node:assert/strict';
const require=createRequire(import.meta.url);
const policy=require(PRELOAD);
const bindings=policy.scopedBindings();
const {writeFileSync}=await import('node:fs');
const {pathToFileURL}=await import('node:url');
const filename=FILE;
writeFileSync(filename, bindings + '\\nexport {spawn, spawnSync};');
const provider=await import(pathToFileURL(filename));
const probe=['--user','--scope','--unit=dsh-subprocess-probe-42-abcdef','--','true'];
const denied=provider.spawnSync('systemd-run',probe);
assert.equal(denied.error.code,'ENOTSUP');
assert.equal(denied.pid,0);
assert.equal(denied.status,null);
assert.throws(()=>provider.spawn('systemd-run',['--user','--scope','--unit=dsh-subprocess-42-abcdef','--','true']), /requires review/);
for (const command of ['systemd-run',CUSTOM]) {
  const foreign=foreignSync(command,probe,{encoding:'utf8'});
  assert.equal(foreign.status,0);
  assert.equal(foreign.stdout.trim(),'foreign-ok');
}
const custom=provider.spawnSync(CUSTOM,probe,{encoding:'utf8'});
assert.equal(custom.status,0);
const ordinary=provider.spawnSync(process.execPath,['-e','console.log("ordinary")'],{encoding:'utf8'});
assert.equal(ordinary.status,0);
assert.equal(ordinary.stdout.trim(),'ordinary');
""".replace("PRELOAD", json.dumps(str(deepseek_cgroup.PRELOAD)))
            code = code.replace("FILE", json.dumps(str(Path(tmp) / "scoped.mjs")))
            code = code.replace("CUSTOM", json.dumps(str(binary)))
            self.run_node(code, dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"]))

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

    def test_profile_source_and_esm_exports_are_verified_without_vendor_dependency(self):
        for absolute, conditional in ((False, False), (True, False), (False, True)):
            with self.subTest(absolute=absolute, conditional=conditional), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                profile = root / "profile"
                provider = profile / "node_modules/@deepseek-ai/dsh-subprocess-local"
                provider.mkdir(parents=True)
                # Keep main distinct and intact; the ESM export selects other bytes.
                (provider / "main.js").write_text('// unchanged declared main\n')
                source = 'console.log("UNREVIEWED PROFILE COMMAND RAN");\n'
                actual = provider / "actual.js"
                actual.write_text(source)
                exports = {"import": "./actual.js", "require": "./main.js"} if conditional else "./actual.js"
                (provider / "package.json").write_text(json.dumps({
                    "name": "@deepseek-ai/dsh-subprocess-local", "version": "0.1.5-rc.2",
                    "type": "module", "main": "main.js", "exports": {".": exports}}))
                importer = profile / "entry.mjs"
                target = actual.as_uri() if absolute else "@deepseek-ai/dsh-subprocess-local"
                importer.write_text("import " + json.dumps(target) + ";\n")
                code = ("const policy=require(" + json.dumps(str(deepseek_cgroup.PRELOAD)) + ");"
                        "policy.installLoader(); import(" + json.dumps(importer.as_uri()) + ");")
                result = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 127, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("WARNING: DeepSeek cgroup protection unavailable", result.stderr)
                self.assertIn(str(provider / "package.json"), result.stderr)
                self.assertIn(hashlib.sha256(source.encode()).hexdigest(), result.stderr)

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
    def invoke(self, mode, preload, profile=None, success=True):
        with tempfile.TemporaryDirectory(prefix="l490-") as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"name":"@deepseek-ai/dsh"}')
            (root / "node_modules").symlink_to(Path(PROVIDER).resolve().parent.parent)
            entry_root = root
            if profile:
                entry_root = root / "profile"
                provider = entry_root / "node_modules/@deepseek-ai/dsh-subprocess-local"
                shutil.copytree(PROVIDER, provider)
                manifest = provider / "package.json"
                pkg = json.loads(manifest.read_text())
                if profile == "version":
                    pkg["version"] = "unexpected"
                elif profile == "source":
                    (provider / "lib/index.js").write_text('console.log("UNREVIEWED RAN");')
                elif profile in ("exports", "import-condition"):
                    (provider / "lib/alternate.js").write_text('console.log("UNREVIEWED RAN");')
                    pkg["exports"]["."] = ({"import": "./lib/alternate.js",
                        "require": "./lib/index.js", "default": "./lib/index.js"}
                        if profile == "import-condition" else {"default": "./lib/alternate.js"})
                manifest.write_text(json.dumps(pkg))
            entry = entry_root / "entry.mjs"
            (entry_root / "provider.yml").write_text("- name: '@deepseek-ai/dsh-subprocess-local'\n")
            shutil.copy(Path(__file__).with_name("l490_provider.mjs"), entry)
            env = os.environ.copy()
            env.pop("NODE_OPTIONS", None)
            if preload:
                env["NODE_OPTIONS"] = deepseek_cgroup.node_options("--max-old-space-size=256")
            result = subprocess.run([NODE, str(entry), PROVIDER, mode], env=env,
                                    capture_output=True, text=True, timeout=20)
            if not success:
                self.assertEqual(result.returncode, 127, result.stderr)
                self.assertNotIn("UNREVIEWED RAN", result.stdout)
                self.assertIn("WARNING: DeepSeek cgroup protection unavailable", result.stderr)
                return result
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip(), "provider returned no report: " + result.stderr)
            return json.loads(result.stdout.splitlines()[-1]), result.stderr

    def test_actual_dsh_loader_checks_profile_override_and_keeps_cgroup(self):
        fixed, _ = self.invoke("boot-ordinary", True, "reviewed")
        self.assertEqual(fixed["groups"], [fixed["parent"]] * 4)
        self.invoke("boot-ordinary", True, "import-condition", success=False)

    def test_profile_provider_and_changed_exports_are_checked_before_execution(self):
        for kind in ("version", "source", "exports", "import-condition"):
            with self.subTest(kind=kind):
                self.invoke("ordinary", True, kind, success=False)

    def test_reviewed_profile_provider_keeps_cgroup(self):
        fixed, _ = self.invoke("ordinary", True, "reviewed")
        self.assertEqual(fixed["groups"], [fixed["parent"]] * 4)

    def test_without_preload_scope_with_preload_inherited_including_setsid(self):
        baseline, _ = self.invoke("ordinary", False)
        fixed, stderr = self.invoke("ordinary", True)
        self.assertIn("/dsh-subprocess-", baseline["groups"][0])
        self.assertNotEqual(baseline["parent"], baseline["groups"][0])
        self.assertEqual(fixed["groups"], [fixed["parent"]] * 4)
        self.assertIn("weaker process-tree containment", stderr)
        self.assertEqual(fixed["nodeOptions"], "--max-old-space-size=256")
        self.assertEqual(fixed["childNodeOptions"], "--max-old-space-size=256")

    def test_pty_keeps_cgroup_and_terminates(self):
        fixed, _ = self.invoke("terminal", True)
        self.assertEqual(fixed["groups"], [fixed["parent"]] * 4)
        self.assertEqual(fixed["childNodeOptions"], "--max-old-space-size=256")

    def test_normal_managed_process_termination_still_works(self):
        fixed, _ = self.invoke("terminate", True)
        self.assertIsNone(fixed["outcome"]["exitCode"])
        self.assertIn(fixed["outcome"]["signal"], ("SIGTERM", "SIGKILL"))
