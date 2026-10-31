# SPDX-License-Identifier: AGPL-3.0-only
"""#5038: kernel witnesses and negative credential/process-identity cases."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import platform
from ameesh.platform import child_environment as E
from ameesh.platform import process as P

SRC = str(Path(__file__).resolve().parents[1] / "src")


class ChildCredentialsTest(unittest.TestCase):
    def test_default_and_explicit_environments_do_not_export_unrelated_secrets(self):
        env = dict(PATH="/bin", OPENAI_API_KEY="account-one", DEEPSEEK_API_KEY_A="account-two",
                   AMEESH_DSN="database", GH_TOKEN="github", PGPASSWORD="postgres",
                   AMEESH_MAX_TOKENS="1000", DATABASE_URL="database-url", DSN="dsn")
        with mock.patch.dict(os.environ, env, clear=True):
            for source in (None, env):
                child = E.for_child(["python3", "helper.py"], source)
                self.assertEqual(child, dict(PATH="/bin", AMEESH_MAX_TOKENS="1000"))
        self.assertIn("OPENAI_API_KEY", env)  # never mutate the parent mapping
        for tool, wanted in (("psql", {"PGPASSWORD"}), ("gh", {"GH_TOKEN"}),
                             ("git", {"GH_TOKEN"}), ("ameesh", {"AMEESH_DSN", "PGPASSWORD"})):
            self.assertEqual({key for key in E.for_child([tool], env)
                              if key not in ("PATH", "AMEESH_MAX_TOKENS")}, wanted)
        self.assertNotIn("GH_TOKEN", E.for_child("gh api", env, shell=True))
        cloud = dict(SCW_SECRET_KEY="scw", GOOGLE_APPLICATION_CREDENTIALS="file",
                     OPENAI_API_KEY="provider")
        self.assertEqual(E.for_child(["scw"], cloud), {"SCW_SECRET_KEY": "scw"})
        self.assertEqual(E.for_child(["gcloud"], cloud), {"GOOGLE_APPLICATION_CREDENTIALS": "file"})

    def test_mesh_child_and_mechanical_wrapper_keep_separate_database_password(self):
        env = E.for_child([sys.executable], os.environ)
        env.update(PYTHONPATH=SRC, AMEESH_DSN="postgresql://mesh@localhost/mesh",
                   PGPASSWORD="synthetic-password", OPENAI_API_KEY="provider")
        script = r'''
import os, pathlib, subprocess, sys, tempfile
from ameesh.platform import child_environment as E
E.install()
with tempfile.TemporaryDirectory() as root:
    tool = pathlib.Path(root, 'ameesh')
    tool.write_text('#!' + sys.executable + '\n' +
        "from ameesh.config import load\nimport os\n" +
        "assert load().dsn == 'postgresql://mesh@localhost/mesh'\n" +
        "assert os.environ['PGPASSWORD'] == 'synthetic-password'\n" +
        "assert 'OPENAI_API_KEY' not in os.environ\n")
    tool.chmod(0o700)
    subprocess.run([str(tool)], check=True)
    subprocess.run([sys.executable, str(tool)],
                   env=E.execution_transport(os.environ), check=True)
'''
        subprocess.run([sys.executable, "-c", script],
                       env=E.grant(env, env), check=True)

    def test_in_process_entrypoint_restores_guard_after_return_and_exception(self):
        from ameesh import main
        from .entrypoint import run
        original = subprocess.Popen.__init__
        with mock.patch("builtins.print"):
            self.assertEqual(run(main.main, ["version"]), 0)
        self.assertIs(subprocess.Popen.__init__, original)

        def fails():
            E.install()
            self.assertIsNot(subprocess.Popen.__init__, original)
            raise SystemExit(7)

        with self.assertRaises(SystemExit):
            run(fails)
        self.assertIs(subprocess.Popen.__init__, original)

    def test_custom_account_source_and_selected_destination(self):
        with mock.patch.object(E, "DECLARED_CREDENTIALS", set()):
            E.declare_accounts({"deepseek": [{"key_env": "ACCOUNT_SOURCE", "env": "ACCOUNT_DEST"}]})
            env = dict(ACCOUNT_SOURCE="source", ACCOUNT_DEST="selected", OPENAI_API_KEY="other",
                       AMEESH_RELAY_SECRET="server-secret")
            self.assertEqual(E.for_child(["helper"], env), {})
            granted = E.harness(env, "deepseek", SimpleNamespace(env="ACCOUNT_DEST"))
            self.assertEqual(E.for_child(["harness"], granted), {"ACCOUNT_DEST": "selected"})
            self.assertEqual(E.for_child(["harness"], granted.copy()), {"ACCOUNT_DEST": "selected"})

    def test_spawn_boundary_including_a_preexisting_subclass(self):
        # The controller receives synthetic keys explicitly, and installs the
        # guard before spawning children through every stdlib convenience API.
        env = E.for_child([sys.executable], os.environ)
        env.update(PYTHONPATH=SRC, OPENAI_API_KEY="synthetic-provider",
                   AMEESH_DSN="synthetic-dsn", GH_TOKEN="synthetic-github")
        script = '''
import json, os, subprocess, sys
class ExistingSubclass(subprocess.Popen):
    pass
from ameesh.platform import child_environment as E
E.install()
E.install()
code = "import json,os;print(json.dumps(sorted(k for k in os.environ if k in ('OPENAI_API_KEY','AMEESH_DSN','GH_TOKEN'))))"
results = []
results.append(subprocess.check_output([sys.executable, '-c', code], text=True).strip())
results.append(subprocess.run([sys.executable, '-c', code], env=dict(os.environ), capture_output=True, text=True).stdout.strip())
with ExistingSubclass([sys.executable, '-c', code], stdout=subprocess.PIPE, text=True) as child:
    results.append(child.communicate()[0].strip())
print(json.dumps(results))
'''
        output = subprocess.check_output([sys.executable, "-c", script],
                                         env=E.grant(env, env), text=True)
        self.assertEqual(json.loads(output), ["[]", "[]", "[]"])


    def test_selected_account_survives_resident_and_acp_transport(self):
        env = E.for_child([sys.executable], os.environ)
        env["PYTHONPATH"] = SRC
        script = r''' 
import json, sys
from types import SimpleNamespace
from ameesh import acp, resident
from ameesh.platform import child_environment as E
E.install()
prepared = E.harness(dict(OPENAI_API_KEY="selected", DEEPSEEK_API_KEY="other",
                          AMEESH_DSN="mesh"), "codex")
driver = resident.ResidentDriver.__new__(resident.ResidentDriver)
driver.extra_env = prepared
driver.descriptor = SimpleNamespace(env={})
driver.adapter = SimpleNamespace(env=lambda: {})
code = "import json,os,sys; request=json.loads(sys.stdin.readline()); print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':sorted(k for k in os.environ if k in ('OPENAI_API_KEY','DEEPSEEK_API_KEY','AMEESH_DSN'))}),flush=True); sys.stdin.read()"
results = []
for environment in (prepared, driver._env()):
    peer = acp.JsonRpcPeer([sys.executable, '-u', '-c', code], env=environment)
    try:
        results.append(peer.request('credentials', timeout=3))
    finally:
        peer.close(grace=0.1)
print(json.dumps(results))
'''
        output = subprocess.check_output([sys.executable, "-c", script], env=env, text=True)
        self.assertEqual(json.loads(output), [["AMEESH_DSN", "OPENAI_API_KEY"]] * 2)

    def test_heavy_preserves_the_callers_exact_environment_with_the_guard_installed(self):
        env = E.for_child([sys.executable], os.environ)
        env.update(PYTHONPATH=SRC, AMEESH_TEST_DSN="synthetic-db",
                   PGPASSWORD="synthetic-pg", GH_TOKEN="synthetic-gh")
        script = """
import os, sys, tempfile
from types import SimpleNamespace
from ameesh import heavy
from ameesh.platform import child_environment as E
E.install()
with tempfile.TemporaryDirectory() as root:
    code = "import os; assert os.environ['AMEESH_TEST_DSN']=='synthetic-db'; assert os.environ['PGPASSWORD']=='synthetic-pg'; assert os.environ['GH_TOKEN']=='synthetic-gh'"
    assert heavy.run(SimpleNamespace(state_dir=root, max_heavy_suites=1), [sys.executable, '-c', code], nice=0, max_s=10) == 0
"""
        subprocess.run([sys.executable, "-c", script], env=env, check=True)

    def test_inherited_invocation_id_does_not_harden_an_ordinary_child(self):
        with mock.patch.dict(os.environ, INVOCATION_ID="inherited"), \
                mock.patch.object(E, "install"), mock.patch.object(E, "harden") as harden, \
                mock.patch.object(P, "_trusted_executor", return_value=False):
            self.assertTrue(E.managed_entry())
            harden.assert_not_called()

    def test_mechanical_scripts_receive_transport_but_no_provider_credentials(self):
        env = E.execution_transport(dict(AMEESH_DSN="db", GH_TOKEN="github",
                                        DEEPSEEK_API_KEY="provider",
                                        SUPABASE_ACCESS_TOKEN="ledger"))
        self.assertEqual(E.for_child(["python3", "composer.py"], env),
                         dict(AMEESH_DSN="db", GH_TOKEN="github"))


class ExecutorIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(P, "PROC", self.tmp.name).start()
        mock.patch.object(P, "_proc_ok", return_value=True).start()
        self.unit = "ameesh-runner-agent@worker.service"
        self.group = "/user.slice/" + self.unit
        self.write_process(91)
        self.write_process(92)

    def write_process(self, pid, *, ticks="1234", group=None):
        root = Path(self.tmp.name, str(pid))
        root.mkdir(exist_ok=True)
        (root / "status").write_text(
            "Name:\tameesh\nState:\tS\nUid:\t" +
            "\t".join([str(os.getuid())] * 4) + "\n")
        fields = ["S"] + ["0"] * 18 + [ticks]
        (root / "stat").write_text("%d (ameesh) %s\n" % (pid, " ".join(fields)))
        (root / "cgroup").write_text("0::" + (group or self.group + "/runner") + "\n")

    def snapshot(self):
        response = "Id=%s\nMainPID=91\nControlGroup=%s\nTransient=no\nFragmentPath=/etc/systemd/system/ameesh-runner@.service\n" % (self.unit, self.group)
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=response)):
            return P.executor_processes()

    def test_only_the_manager_main_pid_is_exempt_not_a_named_child(self):
        snapshot = self.snapshot()
        self.assertTrue(P._trusted_executor(91, lambda: snapshot))
        self.assertFalse(P._trusted_executor(92, lambda: snapshot))
        self.assertIsNone(P._doubt_proc(91, os.getuid(), lambda: set(), executors=lambda: snapshot))
        self.assertIsNotNone(P._doubt_proc(92, os.getuid(), lambda: set(), executors=lambda: snapshot))

    def test_pid_reuse_and_entry_into_a_turn_cgroup_revoke_the_exemption(self):
        snapshot = self.snapshot()
        self.write_process(91, ticks="9999")
        self.assertFalse(P._trusted_executor(91, lambda: snapshot))
        self.write_process(91, group=self.group + "/turn-fake")
        self.assertFalse(P._trusted_executor(91, lambda: snapshot))
        self.assertEqual(self.snapshot(), {})

    def test_system_services_are_found_when_the_user_manager_is_unavailable(self):
        response = "Id=%s\nMainPID=91\nControlGroup=%s\nTransient=no\nFragmentPath=/etc/systemd/system/ameesh-runner@.service\n" % (self.unit, self.group)
        def show(argv, **kwargs):
            if "--user" in argv:
                raise OSError("no user manager")
            self.assertIn("--system", argv)
            return SimpleNamespace(returncode=0, stdout=response)
        with mock.patch("subprocess.run", side_effect=show):
            self.assertTrue(P._trusted_executor(91, P.executor_processes))

    def test_transient_and_missing_fragment_services_are_not_exempt(self):
        for properties in ("Transient=yes\nFragmentPath=/etc/systemd/system/a.service",
                           "Transient=no\nFragmentPath=/run/user/1001/systemd/transient/a.service",
                           "Transient=no\nFragmentPath=", ""):
            response = "Id=%s\nMainPID=91\nControlGroup=%s\n%s\n" % (self.unit, self.group, properties)
            with self.subTest(properties=properties), mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=response)):
                self.assertEqual(P.executor_processes(), {})

    def test_missing_service_manager_grants_nothing(self):
        with mock.patch("subprocess.run", side_effect=OSError("unavailable")):
            self.assertEqual(P.executor_processes(), {})


@unittest.skipUnless(platform.is_linux(), "kernel /proc protection is Linux-only")
class ServiceKernelWitnessTest(unittest.TestCase):
    def test_a_turn_named_ameesh_is_still_an_unread_survivor(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root, "ready")
            code = """
import ctypes, pathlib, sys, time
from ameesh import platform
libc = ctypes.CDLL(None)
libc.prctl(15, b'ameesh', 0, 0, 0)
platform.become_undumpable()
pathlib.Path(sys.argv[1]).write_text('ready')
time.sleep(30)
"""
            env = E.for_child([sys.executable], os.environ)
            # argv[0] deliberately hides the interpreter path. Relocated
            # Python builds (hostedtoolcache in CI) need their real prefixes
            # to find the standard library before executing this helper.
            env.update(PYTHONPATH=SRC, AMEESH_TURN_ID="synthetic-turn",
                       PYTHONHOME=os.pathsep.join((sys.base_prefix, sys.base_exec_prefix)))
            with subprocess.Popen(["ameesh", "-c", code, str(marker)], executable=sys.executable,
                                  env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, text=True) as child:
                try:
                    deadline = time.monotonic() + 10
                    while not marker.exists():
                        if child.poll() is not None:
                            self.fail(child.stderr.read())
                        if time.monotonic() > deadline:
                            self.fail("spoof helper not ready")
                        time.sleep(0.02)
                    start = platform.start_time(child.pid)
                    table = [{"pid": child.pid, "start": start, "zombie": False}]
                    with mock.patch.object(P, "_psutil", return_value=None), \
                            mock.patch.object(P, "process_table", return_value=table), \
                            mock.patch.object(P, "executor_processes", return_value={}):
                        self.assertIn(start, platform.env_values("AMEESH_TURN_ID")["unread"])
                    self.assertIsNotNone(P._doubt_proc(
                        child.pid, os.getuid(), lambda: set(), executors=lambda: {}))
                finally:
                    child.kill()
                    child.wait()

    def test_managed_cli_hides_parent_credentials_and_filters_its_child(self):
        # The service-manager snapshot is injected, the identity and read
        # refusal are checked against a real process by the kernel.
        env = E.for_child([sys.executable], os.environ)
        env.update(PYTHONPATH=SRC, INVOCATION_ID="synthetic-invocation",
                   DEEPSEEK_API_KEY="provider", AMEESH_DSN="transport")
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root, "ready")
            code = """
import os,pathlib,subprocess,sys,time
from ameesh import main,platform
from ameesh.platform import child_environment as E
P = platform.process
identity = P._executor_identity(os.getpid(), platform.self_cgroup_paths()[0])
assert identity is not None
P.executor_processes = lambda: {os.getpid(): identity}
def dispatch(*args):
    subprocess.run([sys.executable, '-c', "import os; assert 'DEEPSEEK_API_KEY' not in os.environ; assert os.environ['AMEESH_DSN']=='transport'"], env=E.execution_transport(os.environ), check=True)
    pathlib.Path(sys.argv[1]).touch()
    return 0
main._dispatch = dispatch
assert main.main(['role','run']) == 0
time.sleep(30)
"""
            with subprocess.Popen([sys.executable, "-c", code, str(marker)], env=env,
                                  stderr=subprocess.PIPE, text=True) as child:
                try:
                    deadline = time.monotonic() + 10
                    while not marker.exists():
                        if child.poll() is not None:
                            self.fail(child.stderr.read())
                        if time.monotonic() > deadline:
                            self.fail("managed CLI did not launch its filtered child")
                        time.sleep(0.02)
                    with self.assertRaises(PermissionError):
                        Path("/proc/%d/environ" % child.pid).read_bytes()
                finally:
                    child.kill()
                    child.wait()

    def test_ordinary_cli_remains_readable_to_holder_and_orphan_scans(self):
        env = E.for_child([sys.executable], os.environ)
        env["PYTHONPATH"] = SRC
        script = """
from unittest import mock
from ameesh import main
import subprocess,sys
from ameesh.platform.child_environment import execution_transport
def dispatch(*args):
    env = execution_transport(dict(DEEPSEEK_API_KEY='provider',GH_TOKEN='transport'))
    subprocess.run([sys.executable,'-c',
        \"import os; assert 'DEEPSEEK_API_KEY' not in os.environ; assert os.environ['GH_TOKEN']=='transport'\"], env=env, check=True)
    return 0
with mock.patch.object(main, '_dispatch', side_effect=dispatch):
    assert main.main(['version']) == 0
"""
        # Main CLI used to harden before dispatch. Observe from a same-uid peer.
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root, "ready")
            script = script + "\nimport pathlib,time\npathlib.Path(%r).touch()\ntime.sleep(30)\n" % str(marker)
            with subprocess.Popen([sys.executable, "-c", script], env=env) as child:
                try:
                    deadline = time.monotonic() + 10
                    while not marker.exists():
                        if child.poll() is not None or time.monotonic() > deadline:
                            self.fail("CLI witness did not finish dispatch")
                        time.sleep(0.02)
                    self.assertTrue(Path("/proc/%d/environ" % child.pid).read_bytes())
                finally:
                    child.kill()
                    child.wait()

    def test_long_running_service_entrypoints_refuse_peer_environment_reads(self):
        for module in ("notify", "serve", "relay", "staging_executor"):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as root:
                marker = Path(root, "ready")
                code = '''
import importlib, pathlib, sys, time
from unittest import mock
from ameesh import config
module = importlib.import_module('ameesh.' + sys.argv[1])
args = {'notify': [], 'serve': [], 'relay': ['serve'], 'staging_executor': ['--config', 'unused', 'tick']}[sys.argv[1]]
try:
    with mock.patch.object(config, 'load', side_effect=RuntimeError('stop after hardening')):
        module.main(args)
except RuntimeError:
    pass
pathlib.Path(sys.argv[2]).write_text('ready')
time.sleep(30)
'''
                env = E.for_child([sys.executable], os.environ)
                env["PYTHONPATH"] = SRC
                with subprocess.Popen([sys.executable, "-c", code, module, str(marker)],
                                      env=env, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, text=True) as child:
                    try:
                        deadline = time.monotonic() + 10
                        while not marker.exists():
                            if child.poll() is not None:
                                self.fail(child.stderr.read())
                            if time.monotonic() > deadline:
                                self.fail("service entrypoint did not reach its configuration")
                            time.sleep(0.02)
                        with self.assertRaises(PermissionError):
                            Path("/proc/%d/environ" % child.pid).read_bytes()
                    finally:
                        child.kill()
                        child.wait()
