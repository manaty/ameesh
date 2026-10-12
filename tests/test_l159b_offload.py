# SPDX-License-Identifier: AGPL-3.0-only
"""Heavy suites offloaded to a lending VM over SSH (lot L159b, mode b1).

The client (`ameesh offload`) talks to the VM side (`ameesh.offload_shell`)
through `tests/fakebin/fake-offload-ssh`, which plays sshd's forced command
locally: real git, real processes, no network, no database.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ameesh import adapters, offload, offload_shell

from .support import REPO

FAKE_SSH = os.path.join(REPO, "tests", "fakebin", "fake-offload-ssh")
AMEESH = os.path.join(REPO, "bin", "ameesh")


def git(cwd: str, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


class OffloadBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="l159b-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.vm = os.path.join(self.tmp, "vm")
        os.makedirs(os.path.join(self.vm, "profiles"))
        self.write_profile("default", {})
        self.config = {"root": self.vm, "slots": 2, "scope": False, "max_job_seconds": 60}
        self.save_config()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "sub"))
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "remote", "add", "origin", "git@github.com:acme/widget.git")
        with open(os.path.join(self.repo, "tracked.txt"), "w") as fh:
            fh.write("v1\n")
        with open(os.path.join(self.repo, ".gitignore"), "w") as fh:
            fh.write("ignored.txt\n")
        with open(os.path.join(self.repo, "sub", "keep"), "w") as fh:
            fh.write("")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "one")

    def write_profile(self, name: str, profile: dict) -> None:
        with open(os.path.join(self.vm, "profiles", name + ".json"), "w") as fh:
            json.dump(profile, fh)

    def save_config(self) -> None:
        self.config_path = os.path.join(self.vm, "offload.json")
        with open(self.config_path, "w") as fh:
            json.dump(self.config, fh)

    def env(self, host: str = "vm") -> dict:
        env = dict(os.environ, AMEESH_OFFLOAD_SSH=FAKE_SSH,
                   AMEESH_FAKE_OFFLOAD_CONFIG=self.config_path,
                   PYTHONPATH=os.path.join(REPO, "src"))
        env.pop("AMEESH_OFFLOAD_HOST", None)
        if host:
            env["AMEESH_OFFLOAD_HOST"] = host
        return env

    def offload(self, *args: str, host: str = "vm", cwd: str | None = None,
                timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([AMEESH, "offload", *args], cwd=cwd or self.repo,
                              env=self.env(host), capture_output=True, text=True,
                              timeout=timeout)

    def journal(self) -> list[dict]:
        path = os.path.join(self.vm, "jobs.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as fh:
            return [json.loads(line) for line in fh]

    def running_jobs(self) -> list[str]:
        jobs = os.path.join(self.vm, "jobs")
        found = []
        for name in os.listdir(jobs) if os.path.isdir(jobs) else ():
            try:
                with open(os.path.join(jobs, name, "meta.json")) as fh:
                    if json.load(fh).get("state") == "running":
                        found.append(name)
            except (OSError, ValueError):
                pass
        return found

    def set_gate(self, state: str, **extra) -> None:
        path = os.path.join(self.tmp, "state.json")
        doc = {"schema": "ameesh-host-state/1", "state": state, "seq": 1, **extra}
        with open(path, "w") as fh:
            json.dump(doc, fh)
        self.config["gate"] = {"mode": "file", "path": path}
        self.save_config()


class RunTest(OffloadBase):
    def test_output_exit_code_and_exact_snapshot(self):
        with open(os.path.join(self.repo, "tracked.txt"), "w") as fh:
            fh.write("v2 uncommitted\n")
        with open(os.path.join(self.repo, "untracked.txt"), "w") as fh:
            fh.write("new\n")
        with open(os.path.join(self.repo, "ignored.txt"), "w") as fh:
            fh.write("secret\n")
        git(self.repo, "add", "tracked.txt")  # staged: the agent's index state
        index_before = git(self.repo, "ls-files", "--stage")
        status_before = git(self.repo, "status", "--porcelain")
        head_before = git(self.repo, "rev-parse", "HEAD")
        res = self.offload("run", "--", "sh", "-c",
                           "cat tracked.txt untracked.txt; test ! -e ignored.txt "
                           "&& echo no-ignored; echo to-stderr >&2; exit 3")
        self.assertEqual(res.returncode, 3, res.stderr)
        self.assertEqual(res.stdout, "v2 uncommitted\nnew\nno-ignored\n")
        self.assertIn("to-stderr", res.stderr)
        self.assertNotIn(offload_shell.PREFIX, res.stderr)  # protocol lines stripped
        self.assertNotIn("warning:", res.stderr)  # no git noise from the checkout
        # neither the index nor the refs of the agent moved
        self.assertEqual(git(self.repo, "ls-files", "--stage"), index_before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), status_before)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head_before)
        self.assertEqual(git(self.repo, "for-each-ref"),
                         git(self.repo, "for-each-ref", "refs/heads", "refs/remotes"))
        # the mirror is named after origin, the job dir is gone, the job journaled
        self.assertTrue(os.path.isdir(os.path.join(self.vm, "mirrors", "acme-widget.git")))
        self.assertEqual(os.listdir(os.path.join(self.vm, "jobs")), [])
        (entry,) = self.journal()
        self.assertEqual((entry["outcome"], entry["exit"]), ("done", 3))

    def test_same_subdirectory_and_private_tmpdir(self):
        res = self.offload("run", "--", "sh", "-c", 'basename "$PWD"; echo "$TMPDIR"',
                           cwd=os.path.join(self.repo, "sub"))
        self.assertEqual(res.returncode, 0, res.stderr)
        name, tmpdir = res.stdout.split()
        self.assertEqual(name, "sub")
        self.assertTrue(tmpdir.startswith(os.path.join(self.vm, "jobs")))

    def test_snapshot_is_exactly_the_working_tree(self):
        # sparse checkout: a tracked credentials file absent from the working
        # tree never reaches the guest; assume-unchanged: the file's real
        # contents travel, not the index's
        os.makedirs(os.path.join(self.repo, "secret"))
        with open(os.path.join(self.repo, "secret", "creds"), "w") as fh:
            fh.write("token\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "creds")
        git(self.repo, "sparse-checkout", "set", "--no-cone", "/*", "!/secret/")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "secret", "creds")))
        git(self.repo, "update-index", "--assume-unchanged", "tracked.txt")
        with open(os.path.join(self.repo, "tracked.txt"), "w") as fh:
            fh.write("edited\n")
        before = git(self.repo, "ls-files", "-v")
        snap = offload.snapshot(self.repo)
        files = git(self.repo, "ls-tree", "-r", "--name-only", snap).split()
        self.assertNotIn("secret/creds", files)
        self.assertEqual(git(self.repo, "show", snap + ":tracked.txt"), "edited")
        self.assertEqual(git(self.repo, "ls-files", "-v"), before)  # agent's index untouched

    def test_second_push_is_differential_and_unchanged_tree_not_resent(self):
        self.assertEqual(self.offload("run", "--", "true").returncode, 0)
        top = self.repo
        snap = offload.snapshot(top)
        self.assertEqual(snap, offload.snapshot(top))  # stable id for an unchanged tree
        with mock.patch.dict(os.environ, AMEESH_OFFLOAD_SSH=FAKE_SSH,
                             AMEESH_FAKE_OFFLOAD_CONFIG=self.config_path,
                             PYTHONPATH=os.path.join(REPO, "src")):
            remote = offload.Remote("vm")
            res = remote.call("put", "acme-widget", "--have", input=b"")
            self.assertEqual(res.stdout.decode().split(), [snap])
            # a new commit: only its objects travel (thin pack against the
            # previous snapshot)
            with open(os.path.join(top, "tracked.txt"), "w") as fh:
                fh.write("v3\n")
            git(top, "commit", "-qam", "two")
            snap2 = offload.snapshot(top)
            pack = subprocess.run(["git", "-C", top, "pack-objects", "--revs", "--thin",
                                   "--shallow", "--stdout", "-q"], input=("%s\n^%s\n" % (snap2, snap)).encode(),
                                  capture_output=True).stdout
            full = subprocess.run(["git", "-C", top, "pack-objects", "--revs", "--stdout",
                                   "-q"], input=(snap2 + "\n").encode(),
                                  capture_output=True).stdout
            self.assertLess(len(pack), len(full))
            offload.push(remote, top, "acme-widget", snap2)
            res = remote.call("put", "acme-widget", "--have", input=b"")
            self.assertEqual(set(res.stdout.decode().split()), {snap, snap2})

    def test_no_history_travels(self):
        with open(os.path.join(self.repo, "credentials.txt"), "w") as fh:
            fh.write("hunter2-secret\n")
        git(self.repo, "add", "credentials.txt")
        git(self.repo, "commit", "-qm", "oops")
        secret = git(self.repo, "rev-parse", "HEAD:credentials.txt")
        git(self.repo, "rm", "-q", "credentials.txt")
        git(self.repo, "commit", "-qm", "remove it")
        res = self.offload("run", "--", "sh", "-c", "test ! -e credentials.txt")
        self.assertEqual(res.returncode, 0, res.stderr)
        mirror = os.path.join(self.vm, "mirrors", "acme-widget.git")
        found = subprocess.run(["git", "-C", mirror, "cat-file", "-e", secret],
                               capture_output=True)
        self.assertNotEqual(found.returncode, 0, "a blob of the history reached the VM")
        self.assertEqual(git(mirror, "rev-list", "--all").split(),
                         [offload.snapshot(self.repo)])  # one parentless commit

    def test_profile_cache_built_once_per_lockfile_and_hooks(self):
        with open(os.path.join(self.repo, "deps.lock"), "w") as fh:
            fh.write("a==1\n")
        self.write_profile("py", {
            "lockfiles": ["deps.lock"],
            "setup": 'echo built >> "$OFFLOAD_CACHE/marker"; echo setup-ran',
            "before": 'echo DB_URL=db-of-$OFFLOAD_JOB',
            "after": 'echo "$OFFLOAD_JOB" >> "%s/after.log"' % self.tmp,
            "env": {"DEPS": "$OFFLOAD_CACHE/marker"}})
        cmd = ["run", "--profile", "py", "--", "sh", "-c", 'cat "$DEPS"; echo "$DB_URL"']
        first = self.offload(*cmd)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("building the dependency cache", first.stderr)
        self.assertEqual(first.stdout.splitlines()[0], "built")
        self.assertTrue(first.stdout.splitlines()[1].startswith("db-of-"))
        second = self.offload(*cmd)
        self.assertEqual(second.stdout.splitlines()[0], "built")  # not rebuilt
        self.assertNotIn("building the dependency cache", second.stderr)
        with open(os.path.join(self.repo, "deps.lock"), "w") as fh:
            fh.write("a==2\n")
        third = self.offload(*cmd)
        self.assertIn("building the dependency cache", third.stderr)
        self.assertEqual(len(os.listdir(os.path.join(self.vm, "cache", "py"))), 4)  # 2 + locks
        with open(os.path.join(self.tmp, "after.log")) as fh:
            self.assertEqual(len(fh.read().split()), 3)

    def test_failed_setup_is_redone(self):
        self.write_profile("bad", {"setup": 'touch "$OFFLOAD_CACHE/half"; exit 4'})
        res = self.offload("run", "--profile", "bad", "--fallback", "none", "--", "true")
        self.assertEqual(res.returncode, offload.EX_TEMPFAIL)
        self.assertIn("setup failed", res.stderr)
        self.assertEqual([n for n in os.listdir(os.path.join(self.vm, "cache", "bad"))
                          if not n.endswith(".lock")], [])

    def test_client_env_cannot_override_host_variables(self):
        res = self.offload("run", "--env", "PATH=/evil", "--fallback", "none", "--", "true")
        self.assertEqual(res.returncode, offload.EX_TEMPFAIL)
        self.assertIn("PATH is set by the host", res.stderr)
        # an empty slice name would drop --cgroup-parent from the Postgres
        # hook: its container would escape MemoryMax and the cleanup
        for env in ("OFFLOAD_SLICE=", "OFFLOAD_SLICE=other.slice"):
            res = self.offload("run", "--env", env, "--fallback", "none", "--", "true")
            self.assertEqual(res.returncode, offload.EX_TEMPFAIL)
            self.assertIn("OFFLOAD_SLICE is set by the host", res.stderr)
        ok = self.offload("run", "--env", "FOO=bar baz", "--", "sh", "-c", 'echo "$FOO"')
        self.assertEqual(ok.stdout, "bar baz\n")

    def test_timeout_is_not_rerun_locally(self):
        marker = os.path.join(self.tmp, "local-run")
        res = self.offload("run", "--timeout", "1", "--", "sh", "-c",
                           'case "$PWD" in %s*) sleep 30;; *) touch %s;; esac'
                           % (self.vm, marker))
        self.assertEqual(res.returncode, 124, res.stderr)
        self.assertIn("timeout", res.stderr)
        self.assertFalse(os.path.exists(marker))

    def test_hooks_and_setup_are_supervised_by_the_job_deadline(self):
        after = os.path.join(self.tmp, "after.log")
        self.write_profile("slowdb", {"before": "sleep 30",
                                      "after": "echo torn-down >> %s" % after})
        self.write_profile("slowdeps", {"lockfiles": [], "setup": "sleep 30"})
        for profile in ("slowdb", "slowdeps"):
            began = time.time()
            res = self.offload("run", "--profile", profile, "--timeout", "2", "--", "true")
            self.assertEqual(res.returncode, 124, res.stderr)
            self.assertLess(time.time() - began, 20, profile)
        with open(after) as fh:
            self.assertEqual(fh.read(), "torn-down\n")  # teardown even after the stop
        # the half-built cache is not kept as ready
        cache = os.path.join(self.vm, "cache", "slowdeps")
        self.assertFalse(any(os.path.exists(os.path.join(cache, d, ".ready"))
                             for d in os.listdir(cache)))


class FallbackTest(OffloadBase):
    def test_a_protocol_look_alike_from_the_command_is_not_a_result(self):
        # the command prints a refused-shaped result line on stderr (the same
        # stream as the protocol), then the connection drops: never taken for
        # the result, so never a local rerun while the suite may still run
        marker = os.path.join(self.tmp, "local-run")
        fake = offload_shell.PREFIX + 'result {"outcome": "refused", "reason": "x"}'
        client = subprocess.Popen(
            [AMEESH, "offload", "run", "--", "sh", "-c",
             'case "$PWD" in %s*) echo \'%s\' >&2; sleep 30;; *) touch %s;; esac'
             % (self.vm, fake, marker)],
            cwd=self.repo, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        self.addCleanup(client.kill)
        deadline = time.time() + 30
        while time.time() < deadline and not self.running_jobs():
            time.sleep(0.2)
        time.sleep(1)  # the look-alike line is out
        subprocess.run(["pkill", "-KILL", "-f", "fake-offload-ssh .*run --nonce [0-9a-f]* acme-widget"])
        _out, err = client.communicate(timeout=30)
        self.assertEqual(client.returncode, offload.EX_TEMPFAIL, err)
        self.assertIn(fake, err)  # relayed as the command's output
        self.assertIn("not rerun on this machine", err)
        self.assertFalse(os.path.exists(marker))

    def test_lost_connection_during_the_job_is_never_rerun_locally(self):
        marker = os.path.join(self.tmp, "local-run")
        client = subprocess.Popen(
            [AMEESH, "offload", "run", "--", "sh", "-c",
             'case "$PWD" in %s*) sleep 30;; *) touch %s;; esac' % (self.vm, marker)],
            cwd=self.repo, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        self.addCleanup(client.kill)
        deadline = time.time() + 30
        while time.time() < deadline and not self.running_jobs():
            time.sleep(0.2)
        self.assertTrue(self.running_jobs())
        # the "network" drops: the ssh process dies, its remote side with it
        subprocess.run(["pkill", "-KILL", "-f", "fake-offload-ssh .*run --nonce [0-9a-f]* acme-widget"])
        _out, err = client.communicate(timeout=30)
        self.assertEqual(client.returncode, offload.EX_TEMPFAIL, err)
        self.assertIn("not rerun on this machine", err)
        self.assertFalse(os.path.exists(marker))
        deadline = time.time() + 15
        while time.time() < deadline and self.running_jobs():
            time.sleep(0.2)
        self.assertEqual(self.running_jobs(), [])  # the VM side cancelled it too

    def test_no_user_manager_refuses_the_job_instead_of_failing_it(self):
        self.config["scope"] = True
        self.save_config()
        run_dir = os.path.join(self.tmp, "run-user")  # no `bus` socket inside
        os.makedirs(run_dir)
        env = dict(self.env(), XDG_RUNTIME_DIR=run_dir)
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        res = subprocess.run([AMEESH, "offload", "run", "--", "sh", "-c", "pwd"],
                             cwd=self.repo, env=env, capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(os.path.realpath(res.stdout.strip()), os.path.realpath(self.repo))
        self.assertIn("no systemd user manager", res.stderr)

    def test_no_host_runs_locally(self):
        res = self.offload("run", "--", "sh", "-c", 'pwd; exit 5', host="")
        self.assertEqual(res.returncode, 5)
        self.assertEqual(os.path.realpath(res.stdout.strip()), os.path.realpath(self.repo))
        self.assertIn("no AMEESH_OFFLOAD_HOST", res.stderr)

    def test_unreachable_host_runs_locally_or_fails_without_fallback(self):
        res = self.offload("run", "--", "sh", "-c", "exit 6", host="unreachable")
        self.assertEqual(res.returncode, 6)
        self.assertIn("running on this machine", res.stderr)
        res = self.offload("run", "--fallback", "none", "--", "true", host="unreachable")
        self.assertEqual(res.returncode, offload.EX_TEMPFAIL)

    def test_gate_not_available_refuses_new_jobs(self):
        for state, extra in (("stopped", {}), ("draining", {"drain_deadline_ts": time.time() + 60}),
                             ("available", {"expires_ts": time.time() - 1})):
            self.set_gate(state, **extra)
            res = self.offload("run", "--", "sh", "-c", 'echo "$PWD"')
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(os.path.realpath(res.stdout.strip()), os.path.realpath(self.repo))
            self.assertIn("refused on vm", res.stderr)
        self.set_gate("available", expires_ts=time.time() + 60)
        res = self.offload("run", "--", "sh", "-c", 'echo "$PWD"')
        self.assertTrue(res.stdout.startswith(self.vm), res.stderr)

    def test_gate_stopped_kills_running_job_then_local_rerun(self):
        self.set_gate("available")
        gate = self.config["gate"]["path"]

        def stop_soon():
            # once the job runs (not before: that would be a refusal)
            deadline = time.time() + 30
            while time.time() < deadline and not self.running_jobs():
                time.sleep(0.2)
            with open(gate, "w") as fh:
                json.dump({"schema": "ameesh-host-state/1", "state": "stopped", "seq": 2,
                           "reason": "app_memory"}, fh)

        threading.Thread(target=stop_soon).start()
        res = self.offload("run", "--", "sh", "-c",
                           'case "$PWD" in %s*) sleep 30;; *) echo local;; esac' % self.vm)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "local\n")
        self.assertIn("app_memory", res.stderr)
        self.assertEqual(self.journal()[-1]["outcome"], "stopped")


class ShellTest(OffloadBase):
    def shell(self, command: str, stdin: bytes = b"") -> subprocess.CompletedProcess:
        env = dict(self.env(), SSH_ORIGINAL_COMMAND=command)
        return subprocess.run([sys.executable, "-m", "ameesh.offload_shell", "--config",
                               self.config_path], input=stdin, env=env, capture_output=True)

    def test_only_four_verbs(self):
        for command in ("", "bash", "sh -c id", "git upload-pack x", "rm -rf /"):
            res = self.shell(command)
            self.assertEqual(res.returncode, 2, command)
            self.assertIn(b"allowed verbs", res.stderr)

    def test_names_and_paths_are_validated(self):
        self.assertIn(b"invalid repository name", self.shell("put ../x --have").stderr)
        self.assertIn(b"invalid commit id", self.shell("put repo zzz").stderr)
        sha = "a" * 40
        res = self.shell("run repo %s --cwd ../.. -- true" % sha)
        self.assertIn(b"inside the repository", res.stderr)
        res = self.shell("run repo %s --profile ../x -- true" % sha)
        self.assertIn(b"unknown commit", res.stderr)  # unknown commit first: nothing runs
        self.assertIn(b"invalid job id", self.shell("status ../x").stderr)

    def test_slots_queue_status_and_cancel(self):
        self.config["slots"] = 1
        self.save_config()
        first = subprocess.Popen([AMEESH, "offload", "run", "--", "sleep", "30"],
                                 cwd=self.repo, env=self.env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.addCleanup(first.kill)
        deadline = time.time() + 20
        while time.time() < deadline and not any(
                j.get("state") == "running" for j in self.status_jobs()):
            time.sleep(0.2)
        second = subprocess.Popen([AMEESH, "offload", "run", "--", "echo", "second"],
                                  cwd=self.repo, env=self.env(), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        self.addCleanup(second.kill)
        deadline = time.time() + 20
        while time.time() < deadline and not any(
                j.get("state") == "waiting" for j in self.status_jobs()):
            time.sleep(0.2)
        (job,) = [j for j in self.status_jobs() if j.get("state") == "running"]
        res = self.offload("cancel", job["job"])
        self.assertEqual(res.returncode, 0, res.stderr)
        out, err = first.communicate(timeout=20)
        self.assertEqual(first.returncode, 130, err)
        self.assertIn("cancelled", err)
        # the freed slot goes to the waiting job
        out, err = second.communicate(timeout=30)
        self.assertEqual((second.returncode, out), (0, "second\n"), err)
        self.assertIn("all slots of vm are busy", err)

    def test_orphan_job_folders_are_pruned(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        orphan = os.path.join(self.vm, "jobs", "20200101-000000-abcdef")
        os.makedirs(orphan)
        with open(os.path.join(orphan, "meta.json"), "w") as fh:
            json.dump({"job": "20200101-000000-abcdef", "state": "running",
                       "shell_pid": dead.pid}, fh)
        self.assertEqual(self.offload("run", "--", "true").returncode, 0)
        self.assertFalse(os.path.exists(orphan))
        self.assertEqual([(e["job"], e["reason"]) for e in self.journal()
                          if e["outcome"] == "error"],
                         [("20200101-000000-abcdef", "orphan job killed and removed")])

    def test_a_reused_pid_does_not_hide_an_orphan(self):
        # the dead shell's PID now belongs to another process: same PID, other
        # start time; the job is an orphan, and cancel never signals that process
        other = subprocess.Popen(["sleep", "60"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        name = "20200101-000000-fedcba"
        orphan = os.path.join(self.vm, "jobs", name)
        os.makedirs(orphan)
        meta = {"job": name, "state": "running", "shell_pid": other.pid,
                "shell_start": "%s:1" % offload_shell._proc_identity(other.pid).split(":")[0]}
        with open(os.path.join(orphan, "meta.json"), "w") as fh:
            json.dump(meta, fh)
        status = [json.loads(line) for line in self.offload("status").stdout.splitlines()]
        self.assertEqual([j["state"] for j in status if j.get("job") == name], ["orphan"])
        self.assertEqual(self.offload("cancel", name).returncode, 0)
        self.assertIsNone(other.poll())  # not signalled
        self.assertEqual(self.offload("run", "--", "true").returncode, 0)
        self.assertFalse(os.path.exists(orphan))
        self.assertIsNone(other.poll())
        # the shell's own identity is recorded and recognised as alive
        me = {"shell_pid": os.getpid(), "shell_start": offload_shell._proc_identity(os.getpid())}
        self.assertTrue(offload_shell._shell_alive(me))

    def test_emit_survives_a_closed_session(self):
        class Gone:
            def write(self, _text):
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self):
                raise BrokenPipeError(32, "Broken pipe")

        with mock.patch.object(sys, "stderr", Gone()):
            offload_shell.emit("result", {"outcome": "done"})  # no exception

    def status_jobs(self) -> list[dict]:
        res = self.offload("status")
        lines = [json.loads(line) for line in res.stdout.splitlines()]
        self.assertIn("host", lines[0])
        return lines[1:]


class GateTest(unittest.TestCase):
    def test_gate_reading_is_closed_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            cfg = {"gate": {"mode": "file", "path": path}}
            self.assertEqual(offload_shell.gate_state(cfg)[0], "stopped")  # absent
            with open(path, "w") as fh:
                fh.write("{not json")
            self.assertEqual(offload_shell.gate_state(cfg)[0], "stopped")
            with open(path, "w") as fh:
                json.dump({"schema": "ameesh-host-state/2", "state": "available"}, fh)
            self.assertEqual(offload_shell.gate_state(cfg)[0], "stopped")
            with open(path, "w") as fh:
                json.dump({"schema": "ameesh-host-state/1", "state": "available",
                           "until_ts": 100}, fh)
            self.assertEqual(offload_shell.gate_state(cfg, now=200)[0], "stopped")
            self.assertEqual(offload_shell.gate_state(cfg, now=50)[0], "available")
            self.assertEqual(offload_shell.gate_state({"gate": {"mode": "none"}})[0],
                             "available")


class AgentNoteTest(unittest.TestCase):
    def test_note_only_when_a_host_is_configured(self):
        self.assertEqual(adapters.offload_note({}), "")
        note = adapters.offload_note({"AMEESH_OFFLOAD_HOST": "vm-tests"})
        self.assertIn("ameesh offload run", note)
        self.assertIn("vm-tests", note)

    def test_mail_turn_carries_the_note(self):
        from ameesh import mail
        with mock.patch.dict(os.environ, {"AMEESH_OFFLOAD_HOST": "vm-tests"}):
            text = mail._assemble("mail", ["— message 1 from a:\n> x"], 0)
        self.assertIn("ameesh offload run", text)
        # one rule for heavy suites: the VM replaces the per-host cap (L131)
        self.assertNotIn(adapters.HEAVY_NOTE, text)
        with mock.patch.dict(os.environ, {"AMEESH_OFFLOAD_HOST": ""}):
            text = mail._assemble("mail", ["— message 1 from a:\n> x"], 0)
        self.assertNotIn("ameesh offload", text)
        self.assertIn(adapters.HEAVY_NOTE, text)


class InstallScriptTest(unittest.TestCase):
    SCRIPT = os.path.join(REPO, "deploy", "guest", "install.sh")

    def test_syntax_and_dry_run_touches_nothing(self):
        subprocess.run(["bash", "-n", self.SCRIPT], check=True)
        res = subprocess.run(["bash", self.SCRIPT, "--dry-run", "--key",
                              "ssh-ed25519 AAAAC3Nza test@poste", "--from", "203.0.113.7"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("useradd", res.stdout)
        self.assertIn('restrict,from="203.0.113.7",command=', res.stdout)
        self.assertIn("MemoryMax=", res.stdout)
        self.assertIn("meta skuid", res.stdout)
        self.assertIn("ForceCommand ", res.stdout)
        self.assertIn(" -I /usr/local/lib/ameesh-guest/ameesh-offload-shell", res.stdout)
        self.assertIn('"profiles": "/etc/ameesh-guest/profiles"', res.stdout)
        self.assertNotIn("No such file", res.stderr)

    def test_shipped_profiles_load(self):
        profiles = os.path.join(REPO, "deploy", "guest", "profiles")
        cfg = {"profiles": profiles}
        names = sorted(f[:-5] for f in os.listdir(profiles) if f.endswith(".json"))
        self.assertIn("default", names)
        for name in names:
            profile = offload_shell.load_profile(cfg, name)
            for hook in ("setup", "before", "after"):
                if profile[hook]:
                    subprocess.run(["sh", "-n", "-c", profile[hook]], check=True)
        subprocess.run(["sh", "-n", os.path.join(REPO, "deploy", "guest", "job-postgres.sh")],
                       check=True)


if __name__ == "__main__":
    unittest.main()
