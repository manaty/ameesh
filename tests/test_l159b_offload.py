# SPDX-License-Identifier: AGPL-3.0-only
"""Heavy suites offloaded to a lending VM over SSH (lot L159b, mode b1).

The client (`ameesh offload`) talks to the VM side (`ameesh.platform.offload_shell`)
through `tests/fakebin/fake-offload-ssh`, which plays sshd's forced command
locally: real git, real processes, no network, no database.
"""
from __future__ import annotations

import ast
import contextlib
import errno
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import adapters, offload, platform
from ameesh.platform import offload_shell

from .support import REPO

FAKE_SSH = os.path.join(REPO, "tests", "fakebin", "fake-offload-ssh")
AMEESH = os.path.join(REPO, "bin", "ameesh")


def setUpModule():
    # L68: the offload shell is the Linux VM side by construction (systemd,
    # /proc, user separation). Its tests run on the Linux fleet only; the
    # macOS job must not pretend to verify an interface macOS never hosts.
    if not platform.is_linux():
        raise unittest.SkipTest("offload (L159b) : le côté VM est Linux seulement")


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

    def wait_for_journal_entry(self, *, exclude: tuple[str, ...] = (),
                               timeout: float = 60.0) -> dict | None:
        """The first terminal journal entry of this VM, bounded, or None.

        The journal is written when the shell has finished with the job,
        before its folder is removed: it is the event that says the
        cancellation was *observed*, not merely that the cleanup ran (#5184).
        `exclude` skips the jobs another scenario of the test left behind
        (the orphan teardown of the regression)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for entry in self.journal():
                if entry.get("job") in exclude:
                    continue
                if entry.get("outcome") in ("cancelled", "done", "error"):
                    return entry
            time.sleep(0.1)
        return None

    def set_gate(self, state: str, **extra) -> None:
        path = os.path.join(self.tmp, "state.json")
        doc = {"schema": "ameesh-host-state/1", "state": state, "seq": 1, **extra}
        with open(path, "w") as fh:
            json.dump(doc, fh)
        self.config["gate"] = {"mode": "file", "path": path}
        self.save_config()

    def drop_the_ssh(self, client: subprocess.Popen) -> None:
        """The "network" drops: this session's ssh dies, its remote side too.

        The kill is scoped to the client's own child (`-P`): CI runs several
        jobs on one host, each running this module, and the global `-f` pattern
        used before (`fake-offload-ssh .*run acme-widget`) reached a concurrent
        job's fake ssh, reddening it with `ssh exit -9` (lot #959).
        """
        subprocess.run(["pkill", "-KILL", "-P", str(client.pid), "-f",
                        "fake-offload-ssh"])


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

    def test_the_client_env_never_reaches_the_profile_hooks(self):
        # B1 of the #354 R2 review: the hooks are root-owned install-side code
        # and run in the job's scope with the user manager; the client's
        # --env there (LD_AUDIT…) would load its library before any filter.
        # The command, the client's own code, keeps it.
        before = os.path.join(self.tmp, "before.env")
        after = os.path.join(self.tmp, "after.env")
        self.write_profile("hooked", {
            "before": 'printf "%%s" "${LD_AUDIT-unset}" > %s' % before,
            "after": 'printf "%%s" "${LD_AUDIT-unset}" > %s' % after})
        res = self.offload("run", "--profile", "hooked",
                           "--env", "LD_AUDIT=./client.so", "--",
                           "sh", "-c", 'printf "%s" "${LD_AUDIT-unset}"')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "./client.so")
        for path in (before, after):
            with open(path) as fh:
                self.assertEqual(fh.read(), "unset")

    def test_timeout_is_not_rerun_locally(self):
        marker = os.path.join(self.tmp, "local-run")
        res = self.offload("run", "--timeout", "1", "--", "sh", "-c",
                           'case "$PWD" in %s*) sleep 30;; *) touch %s;; esac'
                           % (self.vm, marker))
        self.assertEqual(res.returncode, 124, res.stderr)
        self.assertIn("timeout", res.stderr)
        self.assertFalse(os.path.exists(marker))

    def test_wait_timeout_cancels_the_job_and_bounds_the_client(self):
        # #2665/B1 cx2 : --timeout ne borne que le job à partir de son slot ;
        # --wait-timeout borne l'attente locale (slot, transport, push) et
        # annule le job côté VM avant de rendre la main.
        began = time.time()
        res = self.offload("run", "--wait-timeout", "2", "--", "sh", "-c", "sleep 30")
        self.assertEqual(res.returncode, 124, res.stderr)
        if "client wait budget exhausted" in res.stderr:
            # #3877 : runner chargé, le budget est parti avant le job
            # (paquet, envoi, push de l'instantané) : aucun job lancé, rien à
            # annuler. Non concluant (§3), jamais un rouge.
            self.assertEqual(self.running_jobs(), [])
            self.skipTest("wait budget spent before the job started (loaded host)")
        self.assertIn("no result within", res.stderr)
        self.assertLess(time.time() - began, 20)
        # #5184 : l'annulation est asynchrone. Le journal est écrit quand le
        # shell a fini avec le job, avant le ménage du dossier : on attend cet
        # événement (borné), pas la disparition du dossier (un runner chargé
        # peut la retarder), et jamais un job qui a tourné jusqu'au bout. Si
        # le client a rendu la main avant l'annonce `job`, il n'a pas d'id à
        # annuler : c'est au shell de constater la session morte.
        announced = re.search(r"cancelling job (\S+)", res.stderr)
        entry = self.wait_for_journal_entry(timeout=60)
        if announced is not None:
            self.assertIsNotNone(entry, self.journal())
        if entry is not None:
            self.assertEqual(entry["outcome"], "cancelled", entry)
        deadline = time.time() + 10
        while time.time() < deadline and self.running_jobs():
            time.sleep(0.1)
        self.assertEqual(self.running_jobs(), [])

    def test_a_session_lost_before_the_job_is_announced_never_runs_it(self):
        # #5184/#5293 : le budget d'attente peut expirer avant que le shell
        # ait annoncé `job <id>` — au démarrage (le shell est reparenté avant
        # d'entrer dans `cmd_run`) ou pendant l'admission (ménage de
        # l'orphelin) : le client n'a alors aucun id à annuler et ferme la
        # session. Dans les deux cas le shell doit constater la session morte
        # avant `running` et avant la commande, et journaliser une annulation,
        # jamais un job terminé.
        self.write_profile("slow-teardown", {"after": "sleep 3"})
        orphan = "20200101-000000-abcdef"
        orphan_dir = os.path.join(self.vm, "jobs", orphan)
        os.makedirs(orphan_dir)
        dead = subprocess.Popen(["true"])
        dead.wait()
        with open(os.path.join(orphan_dir, "meta.json"), "w") as fh:
            json.dump({"job": orphan, "state": "running", "shell_pid": dead.pid,
                       "shell_start": "dead:1", "profile": "slow-teardown"}, fh)
        marker = os.path.join(self.tmp, "ran")
        began = time.time()
        res = self.offload("run", "--wait-timeout", "2", "--", "sh", "-c",
                           "touch %s; sleep 30" % marker)
        self.assertEqual(res.returncode, 124, res.stderr)
        if "client wait budget exhausted" in res.stderr:
            self.skipTest("wait budget spent before the job started (loaded host)")
        self.assertLess(time.time() - began, 20)
        self.assertIn("no result within", res.stderr)
        # le budget a expiré avant l'admission (démarrage chargé) ou pendant
        # elle (ménage de l'orphelin) : aucun id de job annoncé dans les deux
        # cas, donc aucun cancel envoyé
        self.assertNotIn("cancelling job", res.stderr)
        entry = self.wait_for_journal_entry(exclude=(orphan,), timeout=30)
        self.assertIsNotNone(entry, self.journal())
        self.assertEqual(entry["outcome"], "cancelled", entry)
        self.assertFalse(os.path.exists(marker), "the command ran without a client")
        # the orphan is the fixture that slows admission, not this request:
        # the early path returns before the orphan sweep, so it may outlive the
        # test (the next job cleans it); no job *of this request* may run.
        self.assertEqual([job for job in self.running_jobs() if job != orphan], [])

    def test_a_session_lost_before_main_is_journaled_without_a_job(self):
        # #5184 : le budget d'attente peut expirer pendant le démarrage du
        # shell `run` (CI chargée) : il est reparenté avant que `main`
        # n'enregistre son parent, et seul le flux de protocole dit que la
        # session est morte. La demande est journalisée annulée, sans job ni
        # commande.
        marker = os.path.join(self.tmp, "ran")
        began = time.time()
        with mock.patch.dict(os.environ,
                             {"AMEESH_FAKE_OFFLOAD_START_DELAY": "9"}):
            res = self.offload("run", "--wait-timeout", "8", "--", "sh", "-c",
                               "touch %s; sleep 30" % marker)
        self.assertEqual(res.returncode, 124, res.stderr)
        if "client wait budget exhausted" in res.stderr:
            # #3877 : runner chargé, budget épuisé avant le shell : non
            # concluant (§3), jamais un rouge.
            self.assertEqual(self.running_jobs(), [])
            self.skipTest("wait budget spent before the shell started (loaded host)")
        self.assertLess(time.time() - began, 20)
        self.assertIn("no result within", res.stderr)
        self.assertNotIn("cancelling job", res.stderr)
        entry = self.wait_for_journal_entry(timeout=30)
        self.assertIsNotNone(entry, self.journal())
        self.assertEqual(entry["outcome"], "cancelled", entry)
        self.assertFalse(os.path.exists(marker), "the command ran without a client")
        self.assertEqual(self.running_jobs(), [])

    def test_a_session_gone_before_cmd_run_is_journaled_without_a_job(self):
        # #5184/#5293 : session déjà morte quand le shell atteint `run`
        # (démarrage lent, budget d'attente expiré avant l'admission) : refus
        # à l'entrée, la demande abandonnée est journalisée annulée, aucun
        # dossier de job, aucune commande. C'est le chemin qu'a pris la CI
        # chargée du paquet h-10 (#5293) : sans journal, le test d'intégration
        # attendait 30 s une entrée qui n'arrivait jamais.
        cfg = offload_shell.load_config(self.config_path)
        previous = offload_shell.SESSION_PARENT
        offload_shell.SESSION_PARENT = os.getppid() + 1
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = offload_shell.cmd_run(
                    cfg, ["acme-widget", "a" * 40, "--", "true"])
        finally:
            offload_shell.SESSION_PARENT = previous
        self.assertEqual(rc, 130)
        self.assertIn('"outcome": "cancelled"', err.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.vm, "jobs")))
        (entry,) = self.journal()
        self.assertEqual(entry["outcome"], "cancelled", entry)
        self.assertEqual(entry["state"], "cancelled", entry)
        self.assertEqual(entry["reason"], "SSH session closed before the job started")
        self.assertEqual(entry["argv"], ["true"])

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


class ClientWaitTest(unittest.TestCase):
    def test_the_snapshot_push_is_bounded_before_any_network_call(self):
        # #2665/B1 cx2 : l'échéance du client couvre aussi le push du
        # snapshot, pas seulement l'attente du résultat.
        class Remote:
            def __init__(self):
                self.calls = []

            def call(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                raise AssertionError("aucun appel réseau attendu")

        remote = Remote()
        with self.assertRaises(offload.ClientTimeout):
            offload.push(remote, "/repo", "acme-widget", "a" * 40,
                         deadline=time.monotonic() - 1)
        self.assertEqual(remote.calls, [])

    def test_the_snapshot_construction_is_bounded_before_git(self):
        # #2665/B1 cx2 : même la construction locale du snapshot est bornée.
        with self.assertRaises(offload.SnapshotTimeout):
            offload.snapshot("/repo", deadline=time.monotonic() - 1)


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
        self.drop_the_ssh(client)
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
        # a concurrent CI job runs this module on the same host: its fake ssh
        # carries the same command line and must survive our own drop (#959)
        decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                  FAKE_SSH, "vm", "run", "acme-widget"])
        self.addCleanup(decoy.kill)
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
        # the "network" drops: this session's ssh dies, its remote side too
        self.drop_the_ssh(client)
        _out, err = client.communicate(timeout=30)
        self.assertEqual(client.returncode, offload.EX_TEMPFAIL, err)
        self.assertIn("not rerun on this machine", err)
        self.assertFalse(os.path.exists(marker))
        self.assertIsNone(decoy.poll(), "the fake ssh of a concurrent job was killed")
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
        # The account line depends on the host: install.sh prints useradd when
        # the guest does not exist yet and usermod when it already does (the
        # persistent CI runners, where the pilot installed it). Read the dry run
        # on a guest name no machine carries, so the check does not depend on
        # the state of the host (#954, canon §3).
        guest = "ameesh-guest-dry-run"
        res = subprocess.run(["bash", self.SCRIPT, "--dry-run", "--user", guest,
                              "--key", "ssh-ed25519 AAAAC3Nza test@poste",
                              "--from", "203.0.113.7"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("useradd", res.stdout)
        self.assertIn("usermod -p * " + guest, res.stdout)
        # #4011: the guest's passwd home must be $ROOT, even when the user
        # already exists (install-pilot.sh --offload-only ran first)
        self.assertIn("usermod --home /srv/ameesh-guest " + guest, res.stdout)
        self.assertIn('restrict,from="203.0.113.7",command="exec ', res.stdout)
        self.assertIn("MemoryMax=", res.stdout)
        self.assertIn("meta skuid", res.stdout)
        self.assertIn("ForceCommand ", res.stdout)
        self.assertIn(" -I /usr/local/lib/ameesh-guest/ameesh-offload-shell", res.stdout)
        self.assertIn('"profiles": "/etc/ameesh-guest/profiles"', res.stdout)
        # L159b2: the job user, its launcher and the rule that names the guest
        self.assertIn("Defaults:%s !requiretty" % guest, res.stdout)
        self.assertIn("%s ALL=(ameesh-job) NOPASSWD: "
                      "/usr/local/lib/ameesh-guest/ameesh-job-launcher" % guest, res.stdout)
        self.assertIn('"executor": "job-user"', res.stdout)
        self.assertIn('"job_group": "ameesh-jobs"', res.stdout)
        self.assertIn("usermod -aG ameesh-jobs %s" % guest, res.stdout)
        # B4 of the #354 R3 reading: the payload may not trace its user
        # manager nor the hooks (same UID); raised to 1 at least, never lowered
        self.assertRegex(res.stdout, r"kernel\.yama\.ptrace_scope = [123]\n")
        self.assertRegex(res.stdout, r"\+ sysctl -q -w kernel\.yama\.ptrace_scope=[123]\n")
        for sub in ("mirrors", "cache", "jobs"):
            self.assertIn("install -d -m 2750 -o %s -g ameesh-jobs /srv/ameesh-guest/%s"
                          % (guest, sub), res.stdout)
        self.assertNotIn("No such file", res.stderr)

    def test_dry_run_repairs_an_existing_guest_home(self):
        # #4011: when install-pilot.sh --offload-only ran first, ameesh-guest
        # exists with $ROOT/home; install.sh then gives that home to the job
        # user, so sshd cannot chdir for the guest ("Permission denied", then
        # "lost the connection"). The dry run must show the usermod that moves
        # the guest to $ROOT, before the job's home is installed.
        guest = "ameesh-guest-dry-run"
        real_id = shutil.which("id")
        self.assertIsNotNone(real_id)
        bindir = tempfile.mkdtemp(prefix="l159b-install-id-")
        self.addCleanup(shutil.rmtree, bindir, True)
        fake = os.path.join(bindir, "id")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write(textwrap.dedent("""\
                #!/bin/sh
                if [ "$1" = "-u" ] && [ "$2" = "%s" ]; then
                  echo 997
                  exit 0
                fi
                exec %s "$@"
                """) % (guest, real_id))
        os.chmod(fake, 0o755)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        res = subprocess.run(["bash", self.SCRIPT, "--dry-run", "--user", guest,
                              "--key", "ssh-ed25519 AAAAC3Nza test@poste",
                              "--from", "203.0.113.7"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(res.returncode, 0, res.stderr)
        # the guest is not created again; its passwd home is moved to $ROOT
        self.assertNotIn("ameesh offloaded test jobs (L159b) " + guest, res.stdout)
        self.assertIn("usermod --home /srv/ameesh-guest " + guest, res.stdout)
        # and before the job user's own home is installed over $ROOT/home
        self.assertLess(res.stdout.index("usermod --home /srv/ameesh-guest " + guest),
                        res.stdout.index("install -d -m 0700 -o ameesh-job "
                                         "-g ameesh-jobs /srv/ameesh-guest/home"))

    def test_dry_run_leaves_a_guest_already_at_root_alone(self):
        # #4011: usermod -d refuses a user with live processes (the full
        # pilot's guest runners) even for the same home; under set -e the
        # install would stop there. A guest already at $ROOT is not touched.
        guest = "ameesh-guest-dry-run"
        real_id, real_getent = shutil.which("id"), shutil.which("getent")
        self.assertIsNotNone(real_id)
        self.assertIsNotNone(real_getent)
        bindir = tempfile.mkdtemp(prefix="l159b-install-home-")
        self.addCleanup(shutil.rmtree, bindir, True)
        for name, body in (
                ("id", 'if [ "$1" = "-u" ] && [ "$2" = "%s" ]; then echo 997; exit 0; fi\n'
                       'exec %s "$@"\n' % (guest, real_id)),
                ("getent", 'if [ "$1" = "passwd" ] && [ "$2" = "%s" ]; then\n'
                           '  echo "%s:x:997:997::/srv/ameesh-guest:/bin/sh"; exit 0\n'
                           'fi\nexec %s "$@"\n' % (guest, guest, real_getent))):
            path = os.path.join(bindir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n" + body)
            os.chmod(path, 0o755)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ.get("PATH", ""))
        res = subprocess.run(["bash", self.SCRIPT, "--dry-run", "--user", guest,
                              "--key", "ssh-ed25519 AAAAC3Nza test@poste",
                              "--from", "203.0.113.7"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("usermod --home", res.stdout)

    def test_live_pilot_home_change_has_named_refusal(self):
        # Exercise the real account setup with harmless shell doubles.
        # usermod's refusal must stop installation before any job-home setup,
        # with an actionable diagnostic rather than its raw exit code (9).
        with open(self.SCRIPT, encoding="utf-8") as fh:
            script = fh.read()
        account_setup = script[script.index('say "user $GUEST '):
                               script.index('check_no_power_groups "$GUEST"')]
        harness = textwrap.dedent("""\
            set -e
            GUEST=ameesh-guest
            ROOT=/srv/ameesh-guest
            say() { :; }
            die() { echo "install.sh: $*" >&2; exit 1; }
            id() { echo 997; }
            getent() { echo "ameesh-guest:x:997:997::$ROOT/home:/bin/sh"; }
            run() { "$@"; }
            usermod() {
              if [ "$1" = --home ]; then
                echo "usermod: user ameesh-guest is currently used by process 123" >&2
                return 9
              fi
            }
            """)
        res = subprocess.run(["bash", "-c", harness + account_setup +
                              '\necho job-home-setup-reached\n'],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("guest-home-change refused for ameesh-guest", res.stderr)
        self.assertIn("stop them before rerunning install.sh", res.stderr)
        self.assertNotIn("job-home-setup-reached", res.stdout)

    def test_the_legacy_dry_run_keeps_the_guest_alone(self):
        # --no-job-user: no job user, no group, no launcher, no rule the
        # guest cannot satisfy; the volume stays the guest's own (L159b)
        guest = "ameesh-guest-legacy-dry-run"
        res = subprocess.run(["bash", self.SCRIPT, "--dry-run", "--user", guest,
                              "--no-job-user",
                              "--key", "ssh-ed25519 AAAAC3Nza test@poste"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('"executor": "user-scope"', res.stdout)
        self.assertIn("install -d -m 0700 -o %s -g %s /srv/ameesh-guest" % (guest, guest),
                      res.stdout)
        self.assertNotIn("ameesh-jobs", res.stdout)
        self.assertNotIn("ameesh-job-launcher", res.stdout)
        self.assertNotIn("ptrace_scope", res.stdout)
        self.assertNotIn("No such file", res.stderr)

    def _assert_standalone(self, path):
        # installed alone as /usr/local/lib/ameesh-guest/<name> and run with
        # python3 -I: nothing of ameesh can be imported there
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0, ast.dump(node))
                self.assertNotEqual((node.module or "").split(".")[0], "ameesh")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split(".")[0], "ameesh")

    def test_the_vm_shell_is_standalone(self):
        self._assert_standalone(offload_shell.__file__)

    def test_the_job_launcher_is_standalone(self):
        self._assert_standalone(os.path.join(REPO, "deploy", "guest",
                                             "ameesh-job-launcher.py"))

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


class JobUserExecutorTest(unittest.TestCase):
    """L159b2: the job runs under its own UID, through the root-owned launcher."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="l159b2-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "vm")
        os.makedirs(os.path.join(self.root, "profiles"))
        self.launcher = os.path.join(self.tmp, "ameesh-job-launcher")
        with open(self.launcher, "w") as fh:
            fh.write(textwrap.dedent("""\
                #!/bin/sh
                # test double: keeps the verb, the argv and the environment
                verb=$1; shift
                cat > "$AMEESH_TEST_STDIN"
                printf '%s %s\\n' "$verb" "$*" >> "$AMEESH_TEST_LOG"
                exit 0
                """))
        os.chmod(self.launcher, 0o755)
        self.log = os.path.join(self.tmp, "log")
        self.sent = os.path.join(self.tmp, "stdin")
        self.job_id = "20261014-120000-abcdef"
        self.cfg = {"root": self.root, "slots": 1, "max_job_seconds": 60,
                    "executor": "job-user", "job_user": "ameesh-job",
                    "job_home": os.path.join(self.root, "home"),
                    "launcher": self.launcher,
                    "profiles": os.path.join(self.root, "profiles")}

    def test_load_config_defaults_and_refuses_an_unknown_executor(self):
        path = os.path.join(self.root, "offload.json")
        with open(path, "w") as fh:
            json.dump({"root": self.root, "executor": "job-user"}, fh)
        cfg = offload_shell.load_config(path)
        self.assertEqual(cfg["job_user"], "ameesh-job")
        self.assertEqual(cfg["launcher"], "/usr/local/lib/ameesh-guest/ameesh-job-launcher")
        self.assertEqual(cfg["job_home"], os.path.join(self.root, "home"))
        with open(path, "w") as fh:
            json.dump({"root": self.root, "executor": "wat"}, fh)
        with self.assertRaises(ValueError):
            offload_shell.load_config(path)

    def test_make_executor_picks_the_mode(self):
        self.assertIsInstance(offload_shell.make_executor(dict(self.cfg, executor="user-scope"), "j"),
                              offload_shell.UserScopeExecutor)
        self.assertIsInstance(offload_shell.make_executor(self.cfg, "j"),
                              offload_shell.JobUserExecutor)
        with self.assertRaises(offload_shell.Refused):
            offload_shell.make_executor(dict(self.cfg, executor="nope"), "j")

    def test_argv_goes_through_sudo_and_the_launcher(self):
        ex = offload_shell.make_executor(self.cfg, self.job_id)
        argv = ex.argv("command", ["pytest", "-q"])
        self.assertEqual(argv[:6], ["/usr/bin/sudo", "-n", "-u", "ameesh-job", "--",
                                    self.launcher])
        self.assertEqual(argv[6:11], ["run", "--job", self.job_id, "--phase", "command"])
        self.assertEqual(argv[11:], ["--", "pytest", "-q"])
        self.assertFalse(ex.same_uid)
        self.assertTrue(ex.env_on_stdin)
        self.assertTrue(ex.scope)

    def test_check_refuses_without_the_launcher(self):
        ex = offload_shell.make_executor(dict(self.cfg, launcher=os.path.join(self.tmp, "gone")),
                                         self.job_id)
        with self.assertRaises(offload_shell.Refused):
            ex.check({})

    def test_tune_env_refuses_an_unknown_job_user(self):
        # a VM whose install.sh never created the job user: refused, so the
        # client falls back locally instead of reading a traceback
        ex = offload_shell.make_executor(dict(self.cfg, job_user="no-such-job-user-159b2"),
                                         self.job_id)
        with self.assertRaises(offload_shell.Refused):
            ex.tune_env({})

    def test_grant_adds_group_write_and_keeps_the_exec_bit(self):
        ex = offload_shell.JobUserExecutor(self.cfg, self.job_id)
        tree = os.path.join(self.tmp, "work")
        os.makedirs(os.path.join(tree, "sub"))
        script = os.path.join(tree, "run.sh")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(script, 0o755)
        os.chmod(os.path.join(tree, "sub"), 0o700)
        ex.grant(tree)
        self.assertTrue(os.stat(tree).st_mode & 0o020)
        self.assertTrue(os.stat(tree).st_mode & 0o2000)
        self.assertEqual(os.stat(script).st_mode & 0o770, 0o770)
        self.assertTrue(os.stat(os.path.join(tree, "sub")).st_mode & 0o2000)

    def test_grant_never_chmods_through_a_symlink(self):
        # a checkout may carry symlinks: the walk must not touch their target
        ex = offload_shell.JobUserExecutor(self.cfg, self.job_id)
        tree = os.path.join(self.tmp, "work")
        os.makedirs(tree)
        outside = os.path.join(self.tmp, "outside")
        with open(outside, "w") as fh:
            fh.write("x")
        os.chmod(outside, 0o600)
        os.symlink(outside, os.path.join(tree, "link"))
        ex.grant(tree)
        self.assertEqual(os.stat(outside).st_mode & 0o777, 0o600)

    def test_grant_never_gives_itself_a_path_to_a_swapped_link(self):
        # B3 of the #354 R2 review: a same-UID job can replace an entry while
        # the shell walks a tree it may write. Every entry is opened
        # O_NOFOLLOW and changed on the descriptor, so the swap below makes it
        # unreadable to the walk instead of chmod'ing the link's target.
        ex = offload_shell.JobUserExecutor(self.cfg, self.job_id)
        tree = os.path.join(self.tmp, "work")
        os.makedirs(os.path.join(tree, "sub"))
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        victim = os.path.join(outside, "config")
        with open(victim, "w") as fh:
            fh.write("x")
        os.chmod(victim, 0o600)
        real_listdir = os.listdir
        swapped = []

        def swap(fd):
            names = real_listdir(fd)
            if not swapped:                    # after the listing, before the opens
                swapped.append(True)
                os.rmdir(os.path.join(tree, "sub"))
                os.symlink(outside, os.path.join(tree, "sub"))
            return names

        with mock.patch.object(offload_shell.os, "listdir", swap):
            ex.grant(tree)
        self.assertEqual(os.stat(victim).st_mode & 0o777, 0o600)

    def test_grant_changes_modes_on_the_descriptor_only(self):
        # B3: the fix is fd-based by construction; any return to a path-based
        # os.chmod/os.chown (which follow a swapped link) trips this witness
        ex = offload_shell.JobUserExecutor(self.cfg, self.job_id)
        tree = os.path.join(self.tmp, "work")
        os.makedirs(os.path.join(tree, "sub"))
        with open(os.path.join(tree, "run.sh"), "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(os.path.join(tree, "run.sh"), 0o755)
        with mock.patch.object(offload_shell.os, "chmod",
                               side_effect=AssertionError("path chmod")), \
                mock.patch.object(offload_shell.os, "chown",
                                  side_effect=AssertionError("path chown")):
            ex.grant(tree)
        self.assertTrue(os.stat(tree).st_mode & 0o020)
        self.assertTrue(os.stat(os.path.join(tree, "sub")).st_mode & 0o2000)

    def test_grant_tree_closes_the_descriptors_left_by_a_failing_walk(self):
        # N3 of the #354 R2 review: a raising grant (fstat/fchmod) must not
        # leave the directories still stacked open. The root is granted
        # first, then its four children are opened before the first one is
        # granted: the injected failure happens with a non-empty stack.
        # #1413 P3: the release is proved on the file identity, not on the
        # descriptor number alone -- a concurrent thread could reopen the same
        # number between the close and the check, and the old check would then
        # flag a leak on a descriptor that was released. A number that is
        # closed or names another file counts as released; one that still
        # names the file opened by the walk is a leak.
        tree = os.path.join(self.tmp, "work")
        for name in ("a", "b", "c", "d"):
            os.makedirs(os.path.join(tree, name))
        real_open, real_fchmod = os.open, os.fchmod
        opened: list[tuple[int, int, int]] = []
        grants = {"count": 0}

        def tracking_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            st = os.fstat(fd)
            opened.append((fd, st.st_dev, st.st_ino))
            return fd

        def failing_fchmod(fd, mode):
            grants["count"] += 1
            if grants["count"] == 2:
                raise OSError(errno.EIO, "injected")
            return real_fchmod(fd, mode)

        with mock.patch.object(offload_shell.os, "open", tracking_open), \
                mock.patch.object(offload_shell.os, "fchmod", failing_fchmod):
            with self.assertRaises(OSError):
                offload_shell._grant_tree(tree)
        self.assertGreaterEqual(len(opened), 3)
        for fd, dev, ino in opened:
            try:
                st = os.fstat(fd)
            except OSError:
                continue                        # closed, not left to the caller
            self.assertNotEqual(
                (st.st_dev, st.st_ino), (dev, ino),
                "fd %d still names the file opened by the walk: leaked" % fd)

    def test_granted_requires_the_shared_group_and_write(self):
        # B3: a ready cache is reused only when the shell already granted it
        ex = offload_shell.JobUserExecutor(self.cfg, self.job_id)
        path = os.path.join(self.tmp, "cache")
        os.makedirs(path)
        with mock.patch.object(offload_shell.JobUserExecutor, "_gid",
                               return_value=None):
            os.chmod(path, 0o700)
            self.assertFalse(ex.granted(path))
            os.chmod(path, 0o2770)
            self.assertTrue(ex.granted(path))
        with mock.patch.object(offload_shell.JobUserExecutor, "_gid",
                               return_value=os.getgid() + 1):
            self.assertFalse(ex.granted(path))       # another group: rebuild

    def test_a_legacy_ready_cache_is_rebuilt_not_regranted(self):
        # B3: a cache that does not qualify is never re-granted by path (a
        # same-UID job could have planted a link inside it); it is rebuilt
        ex = offload_shell.JobUserExecutor(self.cfg, self.job_id)
        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        profile = {"lockfiles": [], "setup": "true"}
        final = os.path.join(self.cfg["root"], "cache", "py",
                             offload_shell._lock_hash(work, profile))
        os.makedirs(final)
        with open(os.path.join(final, ".ready"), "w") as fh:
            fh.write("")
        os.chmod(final, 0o700)                   # legacy: the job cannot write it
        job = mock.MagicMock()
        job.cfg, job.executor, job.stop = self.cfg, ex, {}
        job.check.return_value = {}
        runs: list[str] = []

        def run(phase, argv, cwd, env, **kwargs):
            runs.append(phase)
            return 0, ""

        job.run.side_effect = run
        with mock.patch.object(offload_shell.JobUserExecutor, "_gid",
                               return_value=None), \
                mock.patch.object(offload_shell.JobUserExecutor, "clean",
                                  lambda self, path: shutil.rmtree(path, True)):
            self.assertEqual(offload_shell.prepare_cache(job, "py", profile,
                                                         work, {}), final)
            self.assertEqual(runs, ["setup"])    # rebuilt, not reused
            runs.clear()
            # the rebuild granted it: the next run reuses it without touching it
            self.assertEqual(offload_shell.prepare_cache(job, "py", profile,
                                                         work, {}), final)
            self.assertEqual(runs, [])

    def test_dir_keeps_the_setgid_of_a_shared_parent(self):
        # B1 of the #354 R1 review: the explicit chmod of `_dir` must not
        # clear the setgid bit inherited from the volume, or the next level
        # down takes the shell's primary group and `grant` (chmod alone)
        # cannot repair it
        root = os.path.join(self.tmp, "shared")
        os.makedirs(root)
        os.chmod(root, 0o2710)
        leaf = offload_shell._dir(dict(self.cfg, root=root), "cache", "profile",
                                  mode=0o750)
        self.assertEqual(stat.S_IMODE(os.stat(leaf).st_mode), 0o2750)
        self.assertEqual(os.stat(leaf).st_gid, os.stat(root).st_gid)

    def test_dir_does_not_invent_a_setgid_on_a_private_parent(self):
        root = os.path.join(self.tmp, "private")
        os.makedirs(root)
        os.chmod(root, 0o700)
        leaf = offload_shell._dir(dict(self.cfg, root=root), "locks", mode=0o700)
        self.assertEqual(stat.S_IMODE(os.stat(leaf).st_mode), 0o700)

    def test_grant_traverse_leaves_no_group_write(self):
        # B2 of the #354 R1 review: the job enters its folder, but cannot
        # create, rename or remove anything in it (meta.json, cancel)
        job_dir = os.path.join(self.tmp, "job")
        os.makedirs(job_dir, mode=0o700)
        offload_shell.JobUserExecutor(self.cfg, self.job_id).grant_traverse(job_dir)
        mode = os.stat(job_dir).st_mode
        self.assertTrue(mode & 0o2000)        # setgid kept for work/tmp below
        self.assertTrue(mode & 0o050)         # group reads and traverses
        self.assertFalse(mode & 0o020)        # never writes: no planted link

    def test_write_meta_does_not_follow_a_planted_link(self):
        # the job used to plant meta.json.tmp as a link to a shell file: the
        # shell wrote through it under its own UID (B2)
        job_dir = os.path.join(self.tmp, "job")
        os.makedirs(job_dir)
        victim = os.path.join(self.tmp, "victim")
        with open(victim, "w") as fh:
            fh.write("private")
        os.symlink(victim, os.path.join(job_dir, "meta.json.tmp"))
        offload_shell._write_meta(os.path.join(job_dir, "meta.json"), {"job": "x"})
        with open(victim) as fh:
            self.assertEqual(fh.read(), "private")
        meta = os.path.join(job_dir, "meta.json")
        self.assertFalse(os.path.islink(meta))
        with open(meta) as fh:
            self.assertEqual(json.load(fh), {"job": "x"})

    def test_private_writes_never_follow_a_planted_link(self):
        # the cancel flag and the cache's `.ready` go through the same helper
        victim = os.path.join(self.tmp, "victim")
        with open(victim, "w") as fh:
            fh.write("private")
        flag = os.path.join(self.tmp, "cancel")
        os.symlink(victim, flag)
        offload_shell._write_private(flag)
        with open(victim) as fh:
            self.assertEqual(fh.read(), "private")
        self.assertFalse(os.path.islink(flag))

    def test_cancel_does_not_follow_a_planted_link(self):
        cfg = dict(self.cfg)
        job_dir = os.path.join(cfg["root"], "jobs", self.job_id)
        os.makedirs(job_dir)
        victim = os.path.join(self.tmp, "victim")
        with open(victim, "w") as fh:
            fh.write("private")
        os.symlink(victim, os.path.join(job_dir, "cancel"))
        with open(os.path.join(job_dir, "meta.json"), "w") as fh:
            json.dump({"job": self.job_id}, fh)   # no live shell to signal
        self.assertEqual(offload_shell.cmd_cancel(cfg, [self.job_id]), 0)
        with open(victim) as fh:
            self.assertEqual(fh.read(), "private")
        self.assertFalse(os.path.islink(os.path.join(job_dir, "cancel")))

    def _run(self, phase: str, env: dict) -> int:
        job_dir = os.path.join(self.root, "jobs", self.job_id)
        os.makedirs(job_dir, exist_ok=True)
        ex = offload_shell.JobUserExecutor(self.cfg, self.job_id)
        with mock.patch.object(offload_shell.JobUserExecutor, "_launcher",
                               lambda self, *args: [self.launcher, *args]):
            job = offload_shell.Job(self.cfg, self.job_id, job_dir, executor=ex)
            rc, _ = job.run(phase, ["/bin/true"], job_dir, env)
        return rc

    def test_run_sends_the_environment_on_stdin(self):
        env = {"HOME": self.cfg["job_home"], "USER": "ameesh-job", "PATH": "/usr/bin",
               "XDG_RUNTIME_DIR": "/run/user/1001",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus",
               "AMEESH_TEST_STDIN": self.sent, "AMEESH_TEST_LOG": self.log}
        self.assertEqual(self._run("command", dict(env)), 0)
        with open(self.sent) as fh:
            sent = json.load(fh)
        # the client's code never gets the job's user manager
        self.assertNotIn("XDG_RUNTIME_DIR", sent)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", sent)
        self.assertEqual(sent["HOME"], self.cfg["job_home"])
        with open(self.log) as fh:
            self.assertIn("run --job %s --phase command -- /bin/true" % self.job_id, fh.read())

    def test_a_hook_keeps_the_user_manager_for_podman(self):
        env = {"HOME": self.cfg["job_home"], "PATH": "/usr/bin",
               "XDG_RUNTIME_DIR": "/run/user/1001",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus",
               "AMEESH_TEST_STDIN": os.path.join(self.tmp, "hook-stdin"),
               "AMEESH_TEST_LOG": self.log}
        self.assertEqual(self._run("after", dict(env)), 0)
        with open(os.path.join(self.tmp, "hook-stdin")) as fh:
            sent = json.load(fh)
        self.assertEqual(sent["XDG_RUNTIME_DIR"], "/run/user/1001")


class JobLauncherTest(unittest.TestCase):
    """The root-owned launcher's own checks (L159b2)."""

    SCRIPT = os.path.join(REPO, "deploy", "guest", "ameesh-job-launcher")
    MODULE = os.path.join(REPO, "deploy", "guest", "ameesh-job-launcher.py")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="l159b2-launcher-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "vm")
        os.makedirs(os.path.join(self.root, "jobs"))
        spec = importlib.util.spec_from_file_location("ameesh_job_launcher", self.MODULE)
        self.launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.launcher)
        self.launcher.CONFIG = os.path.join(self.tmp, "offload.json")
        with open(self.launcher.CONFIG, "w") as fh:
            json.dump({"root": self.root, "agent_memory_max": "1G"}, fh)

    def test_the_wrapper_is_a_standalone_isolated_invocation(self):
        subprocess.run(["sh", "-n", self.SCRIPT], check=True)
        with open(self.SCRIPT) as fh:
            text = fh.read()
        self.assertIn("-I /usr/local/lib/ameesh-guest/ameesh-job-launcher.py", text)

    def test_the_launcher_names_match_the_shell(self):
        for job_id in ("20261014-120000-abcdef", "20261014-120000-a1b2c3"):
            self.assertEqual(self.launcher.unit_names(job_id),
                             offload_shell.unit_names(job_id))

    def _execv_cmd(self, phase: str) -> list[str]:
        """The argv the launcher hands to systemd-run for that phase."""
        stdin = types.SimpleNamespace(buffer=io.BytesIO(b"{}"))
        # cmd_run clears and replaces os.environ before exec: run it in a copy
        # of the environment, or every later test loses PATH and HOME
        with mock.patch.dict(os.environ), \
                mock.patch.object(self.launcher.os, "execv") as execv, \
                mock.patch.object(self.launcher.sys, "stdin", stdin):
            self.assertEqual(self.launcher.main(
                ["run", "--job", "20261014-120000-abcdef", "--phase", phase,
                 "--", "/bin/true"]), 70)
        return execv.call_args[0][1]

    @unittest.skipIf(os.geteuid() == 0, "the launcher refuses to run as root")
    def test_a_client_phase_runs_under_the_filter(self):
        # B3 of the #354 R1 review: removing the manager's variables was not
        # enough; the payload now runs under the launcher's `exec`, which
        # denies socket(AF_UNIX) before its first instruction
        for phase in ("command", "setup"):
            payload = self._execv_cmd(phase)
            payload = payload[payload.index("--") + 1:]
            self.assertEqual(payload[:4], [sys.executable, "-I", self.MODULE,
                                           "exec"])
            self.assertEqual(payload[4:], ["--", "/bin/true"])
        for phase in ("before", "after"):
            # the profile hooks are root-owned install-side code (Podman needs
            # the manager): they keep it
            payload = self._execv_cmd(phase)
            payload = payload[payload.index("--") + 1:]
            self.assertEqual(payload, ["/bin/true"])

    @unittest.skipIf(os.geteuid() == 0, "the launcher refuses to run as root")
    def test_the_filter_refuses_af_unix_even_when_the_bus_is_reconstructed(self):
        # the exact witness of B3: the payload rebuilds XDG_RUNTIME_DIR and
        # the bus address, then socket(AF_UNIX) is refused; socketpair stays
        # allowed (asyncio, multiprocessing), and a child of the payload
        # inherits the filter, so no child can run the launcher unfiltered
        child = ("import socket\n"
                 "try:\n"
                 "    socket.socket(socket.AF_UNIX)\n"
                 "    print('child allowed')\n"
                 "except OSError as exc:\n"
                 "    print('child', exc.errno)")
        probe = textwrap.dedent("""\
            import os, socket, subprocess, sys
            os.environ["XDG_RUNTIME_DIR"] = "/run/user/UID"
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/UID/bus"
            try:
                socket.socket(socket.AF_UNIX)
            except OSError as exc:
                print("af_unix", exc.errno)
            left, right = socket.socketpair()
            print("socketpair", bool(left))
            left.close()
            right.close()
            print(subprocess.run([sys.executable, "-c", CHILD],
                                 capture_output=True, text=True).stdout.strip())
            """).replace("UID", str(os.getuid())).replace("CHILD", repr(child))
        res = subprocess.run([sys.executable, "-I", self.MODULE, "exec", "--",
                              sys.executable, "-c", probe],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("af_unix %d" % errno.EAFNOSUPPORT, res.stdout)
        self.assertIn("socketpair True", res.stdout)
        self.assertIn("child %d" % errno.EAFNOSUPPORT, res.stdout)

    @unittest.skipIf(os.geteuid() == 0, "the launcher refuses to run as root")
    def test_the_filter_masks_the_domain_to_the_kernels_int(self):
        # B3 of the R2 reading: the kernel reads `domain` as a 32-bit int while
        # libseccomp had compared the whole 64-bit register, so a raw
        # socket(0x100000001) started a real AF_UNIX socket under SCMP_CMP_EQ.
        # The masked rule must refuse the widened domain and keep a widened
        # AF_INET (same high bits, other family) allowed: the mask hides the
        # bits the kernel ignores, it does not block everything above 32 bits.
        try:
            import ctypes
            ctypes.CDLL(self.launcher.SECCOMP_SO)
        except OSError:
            self.skipTest("no %s on this host" % self.launcher.SECCOMP_SO)
        probe = textwrap.dedent("""\
            import ctypes, os, socket
            lib = ctypes.CDLL("libseccomp.so.2")
            lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
            lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
            nr = lib.seccomp_syscall_resolve_name(b"socket")
            libc = ctypes.CDLL(None, use_errno=True)
            libc.syscall.restype = ctypes.c_long
            def raw(domain):
                ctypes.set_errno(0)
                fd = libc.syscall(ctypes.c_long(nr),
                                  ctypes.c_ulonglong(domain),
                                  ctypes.c_ulonglong(socket.SOCK_STREAM),
                                  ctypes.c_ulonglong(0))
                err = ctypes.get_errno()
                if fd >= 0:
                    os.close(fd)
                    return "fd"
                return err
            print("widened-unix", raw(0x100000001))
            print("widened-inet", raw(0x100000002))
            """)
        res = subprocess.run([sys.executable, "-I", self.MODULE, "exec", "--",
                              sys.executable, "-c", probe],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("widened-unix %d" % errno.EAFNOSUPPORT, res.stdout)
        self.assertIn("widened-inet fd", res.stdout)

    @unittest.skipIf(os.geteuid() == 0, "the launcher refuses to run as root")
    def test_the_filter_strips_the_manager_address(self):
        probe = ("import os;print(os.environ.get('XDG_RUNTIME_DIR'),"
                 "os.environ.get('DBUS_SESSION_BUS_ADDRESS'))")
        env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/1000",
                   DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus")
        res = subprocess.run([sys.executable, "-I", self.MODULE, "exec", "--",
                              sys.executable, "-c", probe],
                             capture_output=True, text=True, env=env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.split(), ["None", "None"])

    def test_it_fails_closed_without_an_installable_filter(self):
        with mock.patch.object(self.launcher, "install_socket_filter",
                               side_effect=OSError(errno.ENOSYS, "no libseccomp")), \
                mock.patch.object(self.launcher.os, "execvp") as execvp:
            self.assertEqual(self.launcher.main(["exec", "--", "/bin/true"]), 78)
        execvp.assert_not_called()

    def test_the_self_test_refuses_when_the_filter_is_unavailable(self):
        with mock.patch.object(self.launcher, "install_socket_filter",
                               side_effect=OSError(errno.ENOSYS, "no libseccomp")):
            self.assertEqual(self.launcher.filter_selftest(), 1)

    @unittest.skipIf(os.geteuid() == 0, "the launcher refuses to run as root")
    def test_the_self_test_passes_with_libseccomp(self):
        try:
            import ctypes
            ctypes.CDLL(self.launcher.SECCOMP_SO)
        except OSError:
            self.skipTest("no %s on this host" % self.launcher.SECCOMP_SO)
        self.assertEqual(self.launcher.filter_selftest(), 0)

    def test_it_validates_the_job_and_the_verb(self):
        with self.assertRaises(ValueError):
            self.launcher.job_arg(["--job", "../../etc"])
        with self.assertRaises(ValueError):
            self.launcher.job_arg(["--job"])
        self.assertEqual(self.launcher.job_arg(["--job", "20261014-120000-abcdef"]),
                         "20261014-120000-abcdef")
        self.assertEqual(self.launcher.main(["nope"]), 2)

    def test_clean_stays_inside_the_root(self):
        self.assertEqual(self.launcher.main(["clean", "--path", "/etc"]), 2)
        self.assertEqual(self.launcher.main(["clean", "--path", self.root]), 2)

    @unittest.skipIf(os.geteuid() == 0, "the launcher refuses to run as root")
    def test_clean_empties_a_folder_the_shell_owns(self):
        # the job's folder belongs to the shell: the launcher (the job's UID)
        # cannot unlink it, so it empties it and the shell removes the rest
        victim = os.path.join(self.root, "jobs", "20261014-120000-abcdef")
        os.makedirs(os.path.join(victim, "work", "deep"))
        with open(os.path.join(victim, "work", "deep", "file"), "w") as fh:
            fh.write("x")
        jobs = os.path.dirname(victim)
        os.chmod(jobs, 0o500)
        try:
            self.assertEqual(self.launcher.main(["clean", "--path", victim]), 0)
            self.assertTrue(os.path.isdir(victim))
            self.assertEqual(os.listdir(victim), [])
        finally:
            os.chmod(jobs, 0o755)

    def _check_with_scope(self, scope_path: str) -> tuple[int, str, str]:
        """`main(["check"])` under a fake user manager, with the Yama read
        pointed at `scope_path`; returns (rc, stdout, stderr)."""
        run = os.path.join(self.tmp, "run")
        os.makedirs(os.path.join(run, "bus"), exist_ok=True)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, XDG_RUNTIME_DIR=run), \
                mock.patch.object(self.launcher, "runtime_env"), \
                mock.patch.object(self.launcher, "systemctl",
                                  return_value=subprocess.CompletedProcess([], 0)), \
                mock.patch.object(self.launcher, "filter_selftest", return_value=0), \
                mock.patch.object(self.launcher, "YAMA_SCOPE", scope_path), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.launcher.main(["check"])
        return rc, out.getvalue(), err.getvalue()

    @unittest.skipIf(os.geteuid() == 0, "the launcher refuses to run as root")
    def test_check_refuses_a_kernel_without_yama(self):
        # B4 of the Codex reading: yama_scope() renders None when Yama is
        # missing or unreadable, and `scope is not None and scope < 1`
        # accepted that case, though install.sh --check and the README refuse
        # a kernel without Yama; the filter does not block
        # open(/proc/<pid>/mem), so the launcher must refuse it too
        absent = os.path.join(self.tmp, "absent")
        unreadable = os.path.join(self.tmp, "unreadable")
        os.makedirs(unreadable)  # open() raises OSError, as when it is unreadable
        garbage = os.path.join(self.tmp, "garbage")
        with open(garbage, "w") as fh:
            fh.write("not a number\n")
        for scope in (absent, unreadable, garbage):
            with self.subTest(scope=scope):
                rc, _, err = self._check_with_scope(scope)
                self.assertEqual(rc, 2)
                self.assertIn("kernel.yama.ptrace_scope=None", err)

    @unittest.skipIf(os.geteuid() == 0, "the launcher refuses to run as root")
    def test_check_accepts_scope_one_and_refuses_zero(self):
        zero = os.path.join(self.tmp, "zero")
        with open(zero, "w") as fh:
            fh.write("0\n")
        rc, _, err = self._check_with_scope(zero)
        self.assertEqual(rc, 2)
        self.assertIn("kernel.yama.ptrace_scope=0", err)
        one = os.path.join(self.tmp, "one")
        with open(one, "w") as fh:
            fh.write("1\n")
        rc, out, err = self._check_with_scope(one)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, "ok\n")


if __name__ == "__main__":
    unittest.main()
