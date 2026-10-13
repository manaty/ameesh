# SPDX-License-Identifier: AGPL-3.0-only
"""Heavy suites offloaded to a lending VM over SSH (lot L159b, mode b1).

The client (`ameesh offload`) talks to the VM side (`ameesh.platform.offload_shell`)
through `tests/fakebin/fake-offload-ssh`, which plays sshd's forced command
locally: real git, real processes, no network, no database.
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from ameesh import adapters, offload
from ameesh.platform import offload_shell

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
        subprocess.run(["pkill", "-KILL", "-f", "fake-offload-ssh .*run acme-widget"])
        _out, err = client.communicate(timeout=30)
        self.assertEqual(client.returncode, offload.EX_TEMPFAIL, err)
        self.assertIn(fake, err)  # relayed as the command's output
        self.assertIn("not rerun on this machine", err)
        self.assertFalse(os.path.exists(marker))

    def test_the_job_cannot_read_the_nonce_from_its_ancestors(self):
        # codex r3 on #213: a job of the same UID read the nonce in
        # /proc/<shell>/environ (SSH_ORIGINAL_COMMAND), printed a tagged
        # `refused` and killed the shell: the client fell back to a local run
        # while the job lived on. Here it tries every ancestor's environ and
        # cmdline, and the shell's own stderr, then does the same
        marker = os.path.join(self.tmp, "local-run")
        report = os.path.join(self.tmp, "report.json")
        attack = os.path.join(self.tmp, "attack.py")
        with open(attack, "w") as fh:
            fh.write(textwrap.dedent('''\
                import json, os, re, signal, sys
                found, denied, pid = set(), [], os.getppid()
                shell = pid
                while pid > 1:
                    for name in ("environ", "cmdline"):
                        try:
                            with open("/proc/%%d/%%s" %% (pid, name), "rb") as fh:
                                found.update(re.findall(rb"[0-9a-f]{32}", fh.read()))
                        except OSError:
                            denied.append("%%d/%%s" %% (pid, name))
                    with open("/proc/%%d/stat" %% pid) as fh:
                        pid = int(fh.read().rsplit(")", 1)[1].split()[1])
                try:
                    os.open("/proc/%%d/fd/2" %% shell, os.O_RDONLY | os.O_NONBLOCK)
                except OSError:
                    denied.append("%%d/fd/2" %% shell)
                with open(%r, "w") as fh:
                    json.dump({"shell": shell, "denied": denied,
                               "found": sorted(x.decode() for x in found)}, fh)
                for nonce in found:
                    sys.stderr.write("%s%%s result %%s\\n" %% (nonce.decode(), json.dumps(
                        {"outcome": "refused", "reason": "forged"})))
                sys.stderr.flush()
                os.kill(shell, signal.SIGKILL)
                ''' % (report, offload_shell.PREFIX)))
        client = subprocess.Popen(
            [AMEESH, "offload", "run", "--", "sh", "-c",
             'case "$PWD" in %s*) exec %s %s;; *) touch %s;; esac'
             % (self.vm, sys.executable, attack, marker)],
            cwd=self.repo, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        self.addCleanup(client.kill)
        _out, err = client.communicate(timeout=60)
        with open(report) as fh:
            seen = json.load(fh)
        shell = seen["shell"]
        self.assertIn("%d/environ" % shell, seen["denied"])
        self.assertIn("%d/fd/2" % shell, seen["denied"])
        # argv (cmdline, still readable) carries no nonce: whatever hex it holds
        # (snapshot ids…) forges nothing, the lines are relayed as output
        self.assertEqual(client.returncode, offload.EX_TEMPFAIL, err)
        self.assertIn("not rerun on this machine", err)
        self.assertFalse(os.path.exists(marker))

    def test_a_survivor_of_the_job_cannot_reach_the_protocol_stream(self):
        # r4 on #213: a process of the job that outlives it (a detached child
        # here; systemd-run --user on a VM) read the session's stderr back
        # through /proc/self/fd/2, learnt the nonce from the result line and
        # wrote a forged `refused` (MAC included): the client ran the suite
        # again locally. The job's fds are now pipes of the shell
        marker = os.path.join(self.tmp, "local-run")
        report = os.path.join(self.tmp, "survivor.json")
        attack = os.path.join(self.tmp, "survivor.py")
        with open(attack, "w") as fh:
            fh.write(textwrap.dedent('''\
                import hashlib, hmac, json, os, re, select, sys, time
                # the session's streams: pipes held by an ancestor whose fds
                # are readable (the shell's are not: non-dumpable)
                mine = {os.readlink("/proc/self/fd/%%d" %% n) for n in (1, 2)}
                shared, pid = set(), os.getppid()
                while pid > 1:
                    try:
                        for fd in os.listdir("/proc/%%d/fd" %% pid):
                            link = os.readlink("/proc/%%d/fd/%%s" %% (pid, fd))
                            if link in mine and link.startswith("pipe:"):
                                shared.add(link)
                    except OSError:
                        pass
                    with open("/proc/%%d/stat" %% pid) as fh:
                        pid = int(fh.read().rsplit(")", 1)[1].split()[1])
                if os.fork():
                    time.sleep(0.5)
                    os._exit(0)   # the command ends, its child lives on
                os.setsid()
                if os.fork():
                    os._exit(0)
                found, seen = set(), b""
                fds = []
                for n in (1, 2):
                    try:
                        fds.append(os.open("/proc/self/fd/%%d" %% n,
                                           os.O_RDONLY | os.O_NONBLOCK))
                    except OSError:
                        pass
                end = time.time() + 4
                while time.time() < end and not found:
                    ready, _, _ = select.select(fds, [], [], 0.2)
                    for fd in ready:
                        try:
                            seen += os.read(fd, 65536)
                        except OSError:
                            pass
                    found.update(re.findall(rb"%s([0-9a-f]{32}) ", seen))
                for nonce in found:
                    res = {"job": "x", "outcome": "refused", "reason": "forged"}
                    body = json.dumps(res, sort_keys=True).encode()
                    res["mac"] = hmac.new(nonce, body, hashlib.sha256).hexdigest()
                    try:
                        os.write(2, b"%s" + nonce + b" result "
                                 + json.dumps(res).encode() + b"\\n")
                    except OSError:
                        pass
                with open(%r + ".tmp", "w") as fh:
                    json.dump({"found": sorted(x.decode() for x in found),
                               "shared": sorted(shared)}, fh)
                os.rename(%r + ".tmp", %r)
                ''' % (offload_shell.PREFIX, offload_shell.PREFIX, report, report, report)))
        res = subprocess.run(
            [AMEESH, "offload", "run", "--", "sh", "-c",
             'case "$PWD" in %s*) exec %s %s;; *) touch %s;; esac'
             % (self.vm, sys.executable, attack, marker)],
            cwd=self.repo, env=self.env(), capture_output=True, text=True, timeout=60)
        deadline = time.time() + 20
        while time.time() < deadline and not os.path.exists(report):
            time.sleep(0.2)
        with open(report) as fh:
            seen = json.load(fh)
        self.assertEqual(seen["shared"], [])  # no fd of the session in the job
        self.assertEqual(seen["found"], [])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual(self.journal()[-1]["outcome"], "done")

    def test_a_result_without_a_valid_mac_is_no_result(self):
        # a fake VM shell: the session announce, then a tagged `refused`
        # whose MAC is wrong (or absent): never a local rerun
        marker = os.path.join(self.tmp, "local-run")
        nonce = "ab" * 16
        for mac in ("0" * 64, None):
            with self.subTest(mac=mac):
                result = {"job": "j", "outcome": "refused", "reason": "x"}
                if mac:
                    result["mac"] = mac
                fake = os.path.join(self.tmp, "fake-vm")
                with open(fake, "w") as fh:
                    fh.write(textwrap.dedent('''\
                        #!/bin/sh
                        cat >/dev/null
                        case "$*" in *"run "*)
                          printf '%%s\\n' '%s%s session' '%s%s result %s' >&2;;
                        esac
                        ''' % (offload_shell.PREFIX, nonce, offload_shell.PREFIX, nonce,
                               json.dumps(result))))
                os.chmod(fake, 0o755)
                env = dict(self.env(), AMEESH_OFFLOAD_SSH=fake)
                res = subprocess.run([AMEESH, "offload", "run", "--", "touch", marker],
                                     cwd=self.repo, env=env, capture_output=True,
                                     text=True, timeout=60)
                self.assertEqual(res.returncode, offload.EX_TEMPFAIL, res.stderr)
                self.assertIn("without a valid MAC", res.stderr)
                self.assertFalse(os.path.exists(marker))

    def test_output_without_a_final_newline_does_not_hide_the_result(self):
        # the command's stderr ends mid-line: the result line still starts
        # a line of its own (#312 P2)
        res = self.offload("run", "--", "sh", "-c", "printf partial >&2; printf out")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "out")
        self.assertIn("partial", res.stderr)

    def test_the_client_code_does_not_get_the_user_manager(self):
        # r4 on #213: with XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS, the
        # command or the setup could start a unit outside the job's slice;
        # the profile's hooks keep them (rootless podman)
        self.write_profile("default", {
            "setup": 'echo "setup:${XDG_RUNTIME_DIR-unset}:${DBUS_SESSION_BUS_ADDRESS-unset}" >&2',
            "before": 'echo "before:${XDG_RUNTIME_DIR-unset}" >&2'})
        env = dict(self.env(), XDG_RUNTIME_DIR="/run/user/x", DBUS_SESSION_BUS_ADDRESS="bus")
        res = subprocess.run(
            [AMEESH, "offload", "run", "--", "sh", "-c",
             'echo "cmd:${XDG_RUNTIME_DIR-unset}:${DBUS_SESSION_BUS_ADDRESS-unset}"'],
            cwd=self.repo, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("cmd:unset:unset", res.stdout)
        self.assertIn("setup:unset:unset", res.stderr)
        self.assertIn("before:/run/user/x", res.stderr)
        # with a scope, systemd-run keeps them and the scope's process drops them
        job = offload_shell.Job(dict(self.config, scope=True), "j-1", self.tmp)
        for phase, dropped in (("command", True), ("setup", True), ("before", False)):
            argv = job.argv(phase, ["true"])
            self.assertEqual(argv[0], "systemd-run")
            tail = argv[argv.index("--") + 1:]
            self.assertEqual(tail[:5] == ["/usr/bin/env", "-u", "XDG_RUNTIME_DIR",
                                          "-u", "DBUS_SESSION_BUS_ADDRESS"], dropped, phase)

    def test_a_job_cannot_plant_git_hooks_for_the_shell(self):
        # r5 on #213: the shell's git read ~/.gitconfig and the mirror's
        # config, both writable by a job (same UID): a core.hooksPath planted
        # there ran its hooks outside the slice, with the session's stderr
        home = os.path.join(self.tmp, "home")
        hooks = os.path.join(self.tmp, "hooks")
        os.makedirs(home)
        os.makedirs(hooks)
        marker = os.path.join(self.tmp, "hook-ran")
        for hook in ("post-checkout", "reference-transaction", "pre-auto-gc"):
            path = os.path.join(hooks, hook)
            with open(path, "w") as fh:
                fh.write("#!/bin/sh\necho %s >> %s\n" % (hook, marker))
            os.chmod(path, 0o755)
        mirror = os.path.join(self.vm, "mirrors", "acme-widget.git")
        env = dict(self.env(), HOME=home)
        plant = ('printf "[core]\\n\\thooksPath = %s\\n" > "$HOME/.gitconfig"; '
                 'git config -f %s/config core.hooksPath %s' % (hooks, mirror, hooks))
        res = subprocess.run([AMEESH, "offload", "run", "--", "sh", "-c", plant],
                             cwd=self.repo, env=env, capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(os.path.exists(os.path.join(home, ".gitconfig")))
        with open(os.path.join(self.repo, "tracked.txt"), "w") as fh:
            fh.write("v2\n")  # a new snapshot: put, update-ref, checkout
        res = subprocess.run([AMEESH, "offload", "run", "--", "true"], cwd=self.repo,
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(os.path.exists(marker))

    def test_the_shell_helpers_hold_no_stream_of_the_session(self):
        # r5 on #213: a concurrent job opened /proc/<git>/fd/2 of the shell's
        # git, which held the session's stderr. A `git` first on PATH records,
        # for each call, the pipes it shares with the session (fake ssh)
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        report = os.path.join(self.tmp, "git-fds.jsonl")
        real_git = shutil.which("git")
        with open(os.path.join(bin_dir, "git"), "w") as fh:
            fh.write(textwrap.dedent('''\
                #!%s
                import json, os, sys
                mine = set()
                for n in (0, 1, 2):
                    try:
                        mine.add(os.readlink("/proc/self/fd/%%d" %% n))
                    except OSError:
                        pass
                shared, pid = set(), os.getppid()
                while pid > 1:
                    try:
                        for fd in os.listdir("/proc/%%d/fd" %% pid):
                            link = os.readlink("/proc/%%d/fd/%%s" %% (pid, fd))
                            if link in mine and link.startswith("pipe:"):
                                shared.add(link)
                    except OSError:
                        pass
                    with open("/proc/%%d/stat" %% pid) as st:
                        pid = int(st.read().rsplit(")", 1)[1].split()[1])
                with open(%r, "a") as out:
                    out.write(json.dumps({"argv": sys.argv[1:], "shared": sorted(shared)}) + "\\n")
                os.execv(%r, [%r] + sys.argv[1:])
                ''' % (sys.executable, report, real_git, real_git)))
        os.chmod(os.path.join(bin_dir, "git"), 0o755)
        env = dict(self.env())
        env["PATH"] = bin_dir + os.pathsep + env["PATH"]
        # the client's own git (snapshot) runs on the workstation: only the
        # shell's calls matter, found by their mirror or checkout paths
        res = subprocess.run([AMEESH, "offload", "run", "--", "true"], cwd=self.repo,
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(report) as fh:
            calls = [json.loads(line) for line in fh]
        shell_calls = [c for c in calls if any(self.vm in a for a in c["argv"])]
        self.assertTrue(any("index-pack" in c["argv"] for c in shell_calls), shell_calls)
        self.assertTrue(any("checkout" in c["argv"] for c in shell_calls), shell_calls)
        self.assertEqual([c for c in shell_calls if c["shared"]], [])

    def test_a_session_lingering_after_the_result_is_closed(self):
        # r5 on #213: the client waited for ssh without bound after the result
        nonce = "cd" * 16
        result = {"job": "j", "outcome": "done", "exit": 3}
        result["mac"] = offload_shell.result_mac(nonce, result)
        fake = os.path.join(self.tmp, "fake-vm")
        with open(fake, "w") as fh:
            fh.write(textwrap.dedent('''\
                #!/bin/sh
                cat >/dev/null
                case "$*" in *"run "*)
                  printf '%%s\\n' '%s%s session' '%s%s result %s' >&2
                  exec sleep 300;;
                esac
                ''' % (offload_shell.PREFIX, nonce, offload_shell.PREFIX, nonce,
                       json.dumps(result))))
        os.chmod(fake, 0o755)
        env = dict(self.env(), AMEESH_OFFLOAD_SSH=fake)
        started = time.time()
        res = subprocess.run([AMEESH, "offload", "run", "--", "true"], cwd=self.repo,
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertLess(time.time() - started, offload.RESULT_GRACE_S + 15)
        self.assertEqual(res.returncode, 3, res.stderr)  # the result's exit code
        self.assertIn("did not end after the result", res.stderr)

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
        subprocess.run(["pkill", "-KILL", "-f", "fake-offload-ssh .*run acme-widget"])
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
        return subprocess.run([sys.executable, "-m", "ameesh.platform.offload_shell",
                               "--config", self.config_path], input=stdin, env=env,
                              capture_output=True)

    def test_only_four_verbs(self):
        for command in ("", "bash", "sh -c id", "git upload-pack x", "rm -rf /"):
            res = self.shell(command)
            self.assertEqual(res.returncode, 2, command)
            self.assertIn(b"allowed verbs", res.stderr)

    def test_a_client_nonce_is_refused(self):
        res = self.shell("run --nonce %s acme-widget %s -- true" % ("0" * 32, "0" * 40))
        self.assertEqual(res.returncode, 2)
        self.assertIn(b"--nonce is no longer accepted", res.stderr)

    def test_a_foreign_holder_of_the_session_streams_is_an_error(self):
        # a process of the same UID holding the same ends as the shell's
        # stdout/stderr (here a sibling started by the parent shell, as with
        # a forced command run without exec) could write into the session
        env = dict(self.env(), SSH_ORIGINAL_COMMAND="run acme-widget %s -- true" % ("0" * 40))
        proc = subprocess.Popen(
            ["bash", "-c", 'sleep 60 & echo $! > "$0"; exec "$@"',
             os.path.join(self.tmp, "sibling"), sys.executable, "-m",
             "ameesh.platform.offload_shell", "--config", self.config_path],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        try:
            proc.wait(timeout=60)
        finally:
            # the sibling holds the pipes open: the output ends with it
            with open(os.path.join(self.tmp, "sibling")) as fh:
                os.kill(int(fh.read()), signal.SIGKILL)
        out, err = proc.communicate(timeout=30)
        res = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
        self.assertEqual(res.returncode, 70, res.stderr)
        self.assertIn(b"session streams held by another process", res.stderr)
        results = [line for line in res.stderr.splitlines() if b" result " in line]
        self.assertEqual(len(results), 1, res.stderr)
        result = json.loads(results[0].split(b" result ", 1)[1])
        self.assertEqual(result["outcome"], "error")  # never refused: no local rerun
        self.assertIn("mac", result)
        # the same command, alone on its streams, reaches the job checks
        self.assertEqual(self.shell("run acme-widget %s -- true" % ("0" * 40)).returncode, 2)
        # a shared file on a stream (here /dev/null, open in many processes)
        # is no session stream
        res = subprocess.run([sys.executable, "-m", "ameesh.platform.offload_shell",
                              "--config", self.config_path],
                             env=dict(self.env(), SSH_ORIGINAL_COMMAND="status"),
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_a_planted_promisor_remote_never_runs(self):
        # r6 on #213: a job (same UID) makes the mirror a partial clone whose
        # promisor's uploadpack is a command; a cat-file of a missing object
        # by the shell would lazily fetch, so run it
        missing = "run acme-widget %s -- true" % ("1" * 40)
        self.assertEqual(self.shell(missing).returncode, 2)  # creates the mirror
        mirror = os.path.join(self.vm, "mirrors", "acme-widget.git")
        source = os.path.join(self.tmp, "source.git")
        git(self.tmp, "init", "-q", "--bare", source)
        marker = os.path.join(self.tmp, "upload-pack-ran")
        for key, value in (("core.repositoryformatversion", "1"),
                           ("extensions.partialClone", "evil"),
                           ("remote.evil.url", source),
                           ("remote.evil.promisor", "true"),
                           ("remote.evil.uploadpack", "touch %s; git-upload-pack" % marker)):
            git(mirror, "config", key, value)
        res = self.shell(missing)
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn(b"unknown commit", res.stderr)
        self.assertFalse(os.path.exists(marker))

    def test_a_helper_kept_open_by_its_child_is_bounded(self):
        # r6 on #213: a child keeping the captured stderr blocked the shell
        started = time.monotonic()
        with self.assertRaises(OSError):
            offload_shell._helper(["sh", "-c", "sleep 60 & echo started"], timeout=1)
        self.assertLess(time.monotonic() - started, 10)

    def test_names_and_paths_are_validated(self):
        self.assertIn(b"invalid repository name", self.shell("put ../x --have").stderr)
        self.assertIn(b"invalid commit id", self.shell("put repo zzz").stderr)
        sha = "a" * 40
        res = self.shell("run repo %s --cwd ../.. -- true" % sha)
        self.assertIn(b"inside the repository", res.stderr)
        res = self.shell("run repo %s --profile ../x -- true" % sha)
        self.assertIn(b"unknown commit", res.stderr)  # unknown commit first: nothing runs
        self.assertIn(b"invalid job id", self.shell("status ../x").stderr)

    def wait_for_job_state(self, state: str, what: str, timeout: float) -> list[dict]:
        """The jobs once one of them is in `state`. Fails with the observed
        jobs if `timeout` elapses: a silent timeout used to let the test go on
        with no running job at all on a loaded runner (canon §3: no assertion
        on a fixed delay of the host)."""
        deadline = time.time() + timeout
        while True:
            jobs = self.status_jobs()
            if any(j.get("state") == state for j in jobs):
                return jobs
            if time.time() >= deadline:
                self.fail("%s: no job in state %r after %.0f s (jobs: %s)"
                          % (what, state, timeout, jobs))
            time.sleep(0.2)

    def test_slots_queue_status_and_cancel(self):
        self.config["slots"] = 1
        # the first job runs until cancelled: its admission (checkout, profile
        # cache, hooks) may take tens of seconds on a loaded runner, and a
        # fixed sleep would expire before the assertions
        self.config["max_job_seconds"] = 300
        self.save_config()
        first = subprocess.Popen([AMEESH, "offload", "run", "--", "sleep", "300"],
                                 cwd=self.repo, env=self.env(), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.addCleanup(first.kill)
        self.wait_for_job_state("running", "first job admitted", timeout=120)
        second = subprocess.Popen([AMEESH, "offload", "run", "--", "echo", "second"],
                                  cwd=self.repo, env=self.env(), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        self.addCleanup(second.kill)
        jobs = self.wait_for_job_state("waiting", "second job queued", timeout=60)
        running = [j for j in jobs if j.get("state") == "running"]
        self.assertEqual(len(running), 1, jobs)
        job = running[0]
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

    def _pidfd_job(self, other) -> tuple[dict, str, str]:
        name = "20200101-000000-abcdef"
        job_dir = os.path.join(self.vm, "jobs", name)
        os.makedirs(job_dir)
        with open(os.path.join(job_dir, "meta.json"), "w") as fh:
            json.dump({"job": name, "state": "running", "shell_pid": other.pid,
                       "shell_start": offload_shell._proc_identity(other.pid)}, fh)
        return offload_shell.load_config(self.config_path), name, job_dir

    def test_cancel_signals_through_a_pidfd(self):
        # #312: the identity check and the signal were two steps; a PID
        # reused in between got the SIGTERM. The pidfd opened before the
        # check holds the original shell: here it has died, and the process
        # now at its PID (identity matching) is never signalled
        try:
            probe = os.pidfd_open(os.getpid())
        except (AttributeError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno != errno.ENOSYS:
                raise
            self.skipTest("no pidfd on this kernel or Python")
        os.close(probe)
        other = subprocess.Popen(["sleep", "60"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        gone = subprocess.Popen(["sleep", "60"])
        self.addCleanup(gone.wait)
        self.addCleanup(gone.kill)
        pidfd = os.pidfd_open(gone.pid)
        gone.kill()
        gone.wait()
        cfg, name, job_dir = self._pidfd_job(other)
        with mock.patch.object(offload_shell.os, "pidfd_open", return_value=pidfd), \
                mock.patch.object(sys, "stdout", new=mock.Mock()):
            self.assertEqual(offload_shell.cmd_cancel(cfg, [name]), 0)
        time.sleep(0.2)
        self.assertIsNone(other.poll())  # not signalled
        self.assertTrue(os.path.exists(os.path.join(job_dir, "cancel")))

    def test_cancel_without_pidfd(self):
        other = subprocess.Popen(["sleep", "60"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        cfg, name, _job_dir = self._pidfd_job(other)
        # a pidfd that cannot be opened for another reason: an error, no
        # signal by PID (r1: EPERM, EMFILE fell back to os.kill)
        for code in (errno.EPERM, errno.EMFILE):
            with mock.patch.object(offload_shell.os, "pidfd_open", create=True,
                                   side_effect=OSError(code, os.strerror(code))), \
                    mock.patch.object(sys, "stdout", new=mock.Mock()):
                with self.assertRaises(OSError):
                    offload_shell.cmd_cancel(cfg, [name])
        time.sleep(0.2)
        self.assertIsNone(other.poll())
        # without pidfd support (ENOSYS), the checked PID is still signalled
        with mock.patch.object(offload_shell.os, "pidfd_open", create=True,
                               side_effect=OSError(errno.ENOSYS, "ENOSYS")), \
                mock.patch.object(offload_shell.signal, "pidfd_send_signal", create=True), \
                mock.patch.object(sys, "stdout", new=mock.Mock()):
            offload_shell.cmd_cancel(cfg, [name])
        self.assertEqual(other.wait(timeout=5), -signal.SIGTERM)

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
        self.assertIn('restrict,from="203.0.113.7",command="exec ', res.stdout)
        self.assertIn("MemoryMax=", res.stdout)
        self.assertIn("meta skuid", res.stdout)
        self.assertIn("ForceCommand ", res.stdout)
        self.assertIn(" -I /usr/local/lib/ameesh-guest/ameesh-offload-shell", res.stdout)
        self.assertIn('"profiles": "/etc/ameesh-guest/profiles"', res.stdout)
        self.assertNotIn("No such file", res.stderr)

    def test_the_vm_shell_is_standalone(self):
        # installed alone as /usr/local/lib/ameesh-guest/ameesh-offload-shell
        # and run with python3 -I: nothing of ameesh can be imported there
        import ast
        with open(offload_shell.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, ast.dump(node))
                self.assertNotEqual((node.module or "").split(".")[0], "ameesh")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split(".")[0], "ameesh")

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
