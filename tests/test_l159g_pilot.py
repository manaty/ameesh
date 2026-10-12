# SPDX-License-Identifier: AGPL-3.0-only
"""Pilot (a1) of a lending host (lot L159g, study L159 §2.1, §3, §4.4):
guest units under a capped slice, and the pilot report."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from ameesh import pilot, resources, storage

from .support import REPO, PgTestCase

GUEST = os.path.join(REPO, "deploy", "guest")
GIB = 1024 ** 3


def unit(name: str) -> dict[str, list[str]]:
    """{key: [values]} of a unit file, every section together (systemd keys
    such as `Environment=` may repeat)."""
    out: dict[str, list[str]] = {}
    with open(os.path.join(GUEST, name), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith(("#", "[")):
                continue
            key, _, value = line.partition("=")
            out.setdefault(key, []).append(value)
    return out


class UnitsTest(unittest.TestCase):
    def test_guest_runner(self):
        u = unit("ameesh-guest-runner@.service")
        self.assertEqual(u["User"], ["ameesh-guest"])
        self.assertEqual(u["Slice"], ["ameesh-guest.slice"])
        self.assertEqual(u["OOMScoreAdjust"], ["500"])
        self.assertEqual(u["TimeoutStopSec"], ["180"])
        # a volume in /etc/fstab that did not mount: no start on the root disk
        self.assertEqual(u["RequiresMountsFor"], ["/srv/ameesh-guest"])
        self.assertTrue(u["MemoryHigh"] and u["MemoryMax"])
        self.assertLess(resources.parse_bytes(u["MemoryHigh"][0]),
                        resources.parse_bytes(u["MemoryMax"][0]))
        env = dict(v.partition("=")[::2] for v in u["Environment"])
        self.assertEqual(env["AMEESH_HOST_GATE"], "file")
        self.assertEqual(env["AMEESH_HOST_GATE_FALLBACK"], "stopped")
        self.assertEqual(env["AMEESH_HOST_GATE_PATH"], "/run/ameesh-gate/state/state.json")
        self.assertEqual(env["AMEESH_HOST_GATE_ACK"], "/run/ameesh-gate/ack/ack.json")
        self.assertEqual(env["AMEESH_DRAIN_SECONDS"], "120")
        self.assertTrue(env["HOME"].startswith("/srv/ameesh-guest/"))
        self.assertIn("--agents %i", u["ExecStart"][0])

    def test_guest_runner_hardening(self):
        u = unit("ameesh-guest-runner@.service")
        for key in ("NoNewPrivileges", "PrivateTmp", "PrivateDevices",
                    "ProtectKernelTunables", "ProtectControlGroups", "RestrictSUIDSGID"):
            self.assertEqual(u.get(key), ["yes"], key)
        self.assertEqual(u["ProtectSystem"], ["strict"])
        self.assertEqual(u["ProtectHome"], ["tmpfs"])
        self.assertEqual(u["ProtectProc"], ["invisible"])
        self.assertEqual(u["ReadWritePaths"], ["/srv/ameesh-guest /run/ameesh-gate/ack"])
        self.assertIn("/run/docker.sock", u["InaccessiblePaths"][0])

    def test_guest_slice_yields_to_the_application(self):
        u = unit("ameesh-guest.slice")
        self.assertEqual(u["CPUWeight"], ["10"])
        self.assertEqual(u["IOWeight"], ["10"])
        self.assertTrue(u["CPUQuota"] and u["MemoryMax"] and u["MemoryHigh"])
        # the whole guest holds at least one agent at its ceiling
        runner = unit("ameesh-guest-runner@.service")
        self.assertGreaterEqual(resources.parse_bytes(u["MemoryMax"][0]),
                                resources.parse_bytes(runner["MemoryMax"][0]))

    def test_parent_slice_yields_to_the_application(self):
        # r1 P2: systemd nests the guest slice under ameesh.slice, so its
        # weights only rank it among siblings; the parent's weight is the one
        # that competes with system.slice and user.slice
        self.assertEqual(resources.slice_path("ameesh-guest.slice").split(os.sep)[0],
                         "ameesh.slice")
        u = unit(os.path.join("ameesh.slice.d", "guest-weight.conf"))
        self.assertEqual(u["CPUWeight"], ["10"])
        self.assertEqual(u["IOWeight"], ["10"])
        with open(os.path.join(GUEST, "ameesh-guest.slice"), encoding="utf-8") as fh:
            self.assertIn("ameesh.slice.d/guest-weight.conf", fh.read())

    def test_guest_slice_is_measured(self):
        self.assertIn("ameesh-guest.slice", resources.GUEST_SLICES)


class InstallPilotTest(unittest.TestCase):
    """deploy/guest/install-pilot.sh: what the owner runs on the host (dry
    runs only: nothing is installed by the tests). Every dry run sees a host
    of its own (PILOT_TEST_HOST): never the real /etc/ameesh, the real
    ameesh-guest user, mounts or systemd units, whatever this machine has."""
    SCRIPT = os.path.join(GUEST, "install-pilot.sh")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.host = os.path.realpath(tmp.name)
        for d in ("etc", "proc/self", "opt", "srv", "run"):
            os.makedirs(os.path.join(self.host, d))
        with open(os.path.join(self.host, "etc", "passwd"), "w", encoding="utf-8") as fh:
            fh.write("root:x:0:0:root:/root:/bin/bash\n")
        # "/" only: /srv/ameesh-guest is not a mount point
        self.mountinfo(os.path.join(self.host, "proc", "self"))

    def env(self, **extra):
        return dict(os.environ, PILOT_TEST_HOST=self.host, **extra)

    def run_script(self, *args, stdin=None, **extra):
        return subprocess.run(["bash", self.SCRIPT, "--dry-run", *args], capture_output=True,
                              text=True, input=stdin, env=self.env(**extra))

    def dry(self, *args, stdin=None, **extra):
        return self.run_script("--host", "vm-test", *args, stdin=stdin, **extra)

    def on_host(self, path, content=None):
        """PATH made in the test's host: a directory, or a file with CONTENT."""
        full = self.host + path
        if content is None:
            os.makedirs(full, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
        return full

    def test_guard_unit(self):
        u = unit("ameesh-guard.service")
        self.assertNotIn("User", u)                      # root: set-property, freeze, kill
        self.assertEqual(u["Slice"], ["system.slice"])   # never inside the slice it caps
        self.assertIn("guard run $AMEESH_GUARD_ARGS", u["ExecStart"][0])
        env = dict(v.partition("=")[::2] for v in u["Environment"])
        self.assertEqual(env["AMEESH_GUARD_ARGS"], "--observe")
        self.assertEqual(env["AMEESH_GUARD_CONFIG"], "/etc/ameesh/guard.json")

    def test_gate_directories(self):
        with open(os.path.join(GUEST, "ameesh-gate.tmpfiles.conf"), encoding="utf-8") as fh:
            lines = [l.split() for l in fh if l.strip() and not l.startswith("#")]
        dirs = {l[1]: l[2:5] for l in lines}
        self.assertEqual(dirs["/run/ameesh-gate/state"], ["0755", "root", "root"])
        self.assertEqual(dirs["/run/ameesh-gate/ack"][1:], ["ameesh-guest", "ameesh-guest"])
        # the runner's ReadWritePaths needs the ack directory to exist
        runner = unit("ameesh-guest-runner@.service")
        self.assertIn("/run/ameesh-gate/ack", runner["ReadWritePaths"][0])

    def test_install_dry_run(self):
        subprocess.run(["bash", "-n", self.SCRIPT], check=True)
        res = self.dry("--allow-rootfs", "--agent", "deepseek5")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("useradd --system", out)
        self.assertIn('"host": "vm-test"', out)
        self.assertIn("AMEESH_GUARD_ARGS=--observe", out)
        self.assertIn("/etc/ameesh/guest-deepseek5.env (0640 root:ameesh-guest)", out)
        # the offer is checked by the installed guard before it is enabled
        self.assertLess(out.index("guard check --config /etc/ameesh/guard.json"),
                        out.index("systemctl enable --now ameesh-guard.service"))
        # no guest runner is started by the install
        self.assertNotIn("+ systemctl enable --now ameesh-guest-runner@", out)

    def test_the_dry_run_reads_the_test_host_only(self):
        # mesh-design #436: the tests never read the machine they run on
        # (its /etc/ameesh, its ameesh-guest user, its mounts, its units)
        out = self.dry().stdout
        self.assertIn("+ useradd --system", out)
        self.assertIn("warning: /srv/ameesh-guest is not a mount point", out)
        self.assertIn("+ record 'secret /etc/ameesh/guest.env'", out)
        self.assertIn("+ record 'guest-dir /srv/ameesh-guest'", out)
        # the same test host, with the user, a secrets file, the volume
        # mounted and an enforcing guard installed: seen there, not elsewhere
        with open(os.path.join(self.host, "etc", "passwd"), "a", encoding="utf-8") as fh:
            fh.write("ameesh-guest:x:998:998::/srv/ameesh-guest/home:/bin/bash\n")
        self.on_host("/etc/ameesh/guest.env", "AMEESH_DSN=x\n")
        self.on_host("/etc/ameesh/guard.env", "AMEESH_GUARD_ARGS=\n")
        self.on_host("/srv/ameesh-guest/home")
        self.mountinfo(os.path.join(self.host, "proc", "self"), "/srv/ameesh-guest")
        res = self.dry()
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("user ameesh-guest exists: kept as is", out)
        self.assertNotIn("useradd", out)
        self.assertNotIn("not a mount point", out)
        self.assertIn("kept /etc/ameesh/guest.env", out)
        self.assertNotIn("guest-dir /srv/ameesh-guest'", out)
        self.assertNotIn("guest-tree /srv/ameesh-guest/home'", out)
        self.assertIn("AMEESH_GUARD_ARGS=\n", out)     # the installed mode kept

    def test_an_invalid_offer_is_refused(self):
        res = self.dry("--allow-rootfs", "--lend", "/dev/stdin", stdin='{"cpu_max": 2}')
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("`memory_max` required", res.stderr)

    def test_arguments_are_checked(self):
        for args in (["--enable", "x;rm"], ["--purge"], ["--set-guard-mode"],
                     ["--set-guard-mode", "on"], ["--root", "srv"]):
            res = self.dry(*args)
            self.assertNotEqual(res.returncode, 0, args)

    def test_enable_and_guard_mode(self):
        res = self.dry("--enable", "deepseek5", "deepseek6", *self.workspace_args())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl enable --now ameesh-guest-runner@deepseek6.service", res.stdout)
        res = self.dry("--set-guard-mode", "enforce")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("AMEESH_GUARD_ARGS=\n", res.stdout)
        self.assertIn("systemctl restart ameesh-guard.service", res.stdout)

    # --- the guests' workspace (#483): clone, git credential, dsh profile ---

    REPO_ARG = "proj-a=https://github.com/example-org/proj-a.git#develop"
    TOKEN = "ghp_" + "T0k3n" * 6

    def canon(self, work="/srv/ameesh-guest/work/{agent}", agents=("deepseek5", "deepseek6"),
              placed=("deepseek5", "deepseek6"), team="proj-a"):
        """A canon of the test's own, a git checkout read at origin/main: the
        agents on project TEAM, their work folders on host vm-test."""
        root = os.path.join(self.host, "canon")
        if os.path.isdir(root):
            subprocess.run(["rm", "-rf", root], check=True)
        def fiche(path, text):
            os.makedirs(os.path.dirname(os.path.join(root, path)), exist_ok=True)
            with open(os.path.join(root, path), "w", encoding="utf-8") as fh:
                fh.write("---\n%s---\n" % text)
        for a in agents:
            fiche("agents/%s.md" % a, "type: Agent\ntitle: %s\nresponsible: human:alice\n"
                  "team: %s\nharness: deepseek\nprovider: deepseek\n"
                  "credential_mode: api-key\n" % (a, team))
        for a in placed:
            fiche("placements/%s-vm-test.md" % a, "type: Placement\ntitle: %s@vm-test\n"
                  "agent: %s\nhost: vm-test\ncredential_mode: api-key\n" % (a, a))
        fiche("hotes/vm-test.md", "type: Host\ntitle: vm-test\nresponsible: human:alice\n"
              "policy:\n  harnesses: [deepseek]\n  max_agents: 2\n  work_dirs:\n"
              + "".join('    %s: "%s"\n' % (a, work) for a in agents))
        git = ["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", root], check=True)
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "canon"], check=True)
        subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        return root

    def dsh(self, **files):
        """A ~/.dsh of the test's own: hooks.json, a profile, and what must
        never be copied (a session, a key, a log)."""
        root = os.path.join(self.host, "dsh-source")
        content = {"hooks.json": '{"hooks": {"Stop": [{"hooks": [{"type": "command", '
                                 '"command": "/usr/local/bin/agent-mail hook deepseek"}]}]}}',
                   "profiles/agent/cordis.yml": "[]\n",
                   "profiles/agent/package.json": '{"name": "dsh-profile-agent"}\n',
                   "profiles/agent/.plugin-manager/logs/op.log": "x\n",
                   "llm-deepseek/key": "sk-" + "s3cr3t" * 5 + "\n",
                   "sessions/1.json": "{}\n", **files}
        for rel, text in content.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return root

    def workspace_args(self, *, repo=True, dsh=True):
        root = os.path.join(self.host, "canon")
        if not os.path.isdir(root):
            self.canon()
        args = ["--canon", root]
        if repo:
            args += ["--repo", self.REPO_ARG]
        if dsh:
            args += ["--dsh-from", self.dsh()]
        return args

    def guest_user(self):
        """ameesh-guest in the test's passwd, with the test's own uid (its
        files, made by the test, are then the guest's)."""
        with open(os.path.join(self.host, "etc", "passwd"), "a", encoding="utf-8") as fh:
            fh.write("ameesh-guest:x:%d:%d::/srv/ameesh-guest/home:/bin/bash\n"
                     % (os.getuid(), os.getgid()))

    def test_enable_prepares_the_workspace(self):
        self.on_host("/etc/ameesh/guest-deepseek6.env",
                     "DEEPSEEK_API_KEY=k\nGH_TOKEN=%s\n" % self.TOKEN)
        res = self.dry("--enable", "deepseek6", *self.workspace_args())
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        # the helper, root's; the guest's ~/.gitconfig names it, set by the guest
        self.assertIn("--- /opt/ameesh/git-credential-guest (0755 root:root)", out)
        # the clone's helpers, in its own .git/config: reset, then ours alone
        self.assertIn("+ runuser -u ameesh-guest -- git -C /srv/ameesh-guest/work/deepseek6 config "
                      "--local --replace-all credential.helper \n", out)
        self.assertIn("+ runuser -u ameesh-guest -- git -C /srv/ameesh-guest/work/deepseek6 config "
                      "--local --add credential.helper /opt/ameesh/git-credential-guest\n", out)
        # the branch asked of the forge before anything is written
        self.assertLess(out.index("ls-remote --exit-code --heads -- "
                                  "https://github.com/example-org/proj-a.git refs/heads/develop"),
                        out.index("--- /opt/ameesh/git-credential-guest"))
        # the profile: hooks.json and the profile's plain files, nothing else
        tar = next(l for l in out.splitlines() if l.startswith("+ (dsh_read tar "))
        self.assertIn(" hooks.json profiles/agent/cordis.yml profiles/agent/package.json) |", tar)
        for never in ("plugin-manager", "llm-deepseek", "sessions"):
            self.assertNotIn(never, tar)
        self.assertIn("| runuser -u ameesh-guest -- tar -C /srv/ameesh-guest/home/.dsh -xf -", tar)
        # the clone, from the canon's project and folder, by the guest, detached
        self.assertIn(" git -c credential.helper= -c credential.helper=", out)
        self.assertIn(" -C /srv/ameesh-guest/work clone --quiet "
                      "--no-checkout -- https://github.com/example-org/proj-a.git "
                      "/srv/ameesh-guest/work/deepseek6", out)
        self.assertIn("+ runuser -u ameesh-guest -- git -C /srv/ameesh-guest/work/deepseek6 "
                      "checkout --quiet --detach origin/develop", out)
        # all of it before the runner starts; the token never printed
        self.assertLess(out.index("checkout --quiet --detach"),
                        out.index("+ systemctl enable --now ameesh-guest-runner@deepseek6"))
        self.assertNotIn(self.TOKEN, out + res.stderr)
        self.assertNotIn("GH_TOKEN not set", out + res.stderr)
        # without the token: said in a dry run, refused for real
        os.remove(self.host + "/etc/ameesh/guest-deepseek6.env")
        res = self.dry("--enable", "deepseek6", *self.workspace_args())
        self.assertIn("GH_TOKEN not set in /etc/ameesh/guest-deepseek6.env (refused for real)",
                      res.stdout)

    def test_the_helper_hands_the_token_to_github_only(self):
        out = self.dry("--enable", "deepseek6", *self.workspace_args()).stdout
        text = out.split("--- /opt/ameesh/git-credential-guest (0755 root:root)\n")[1]
        helper = self.on_host("/opt/ameesh/git-credential-guest", text.split("\n---\n")[0] + "\n")
        os.chmod(helper, 0o755)
        def fill(host, token=self.TOKEN):
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_TOKEN=token,
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            res = subprocess.run(
                ["git", "-c", "credential.helper=",
                 "-c", "credential.https://github.com.helper=" + helper, "credential", "fill"],
                input="protocol=https\nhost=%s\npath=example-org/proj-a.git\n\n" % host,
                capture_output=True, text=True, env=env)
            return res.stdout
        self.assertIn("username=x-access-token\npassword=%s\n" % self.TOKEN, fill("github.com"))
        self.assertNotIn(self.TOKEN, fill("example.com"))
        res = subprocess.run([helper, "get"], input="protocol=https\nhost=evil.example\n\n",
                             capture_output=True, text=True, env=dict(os.environ, GH_TOKEN="x"))
        self.assertEqual(res.stdout, "")

    def test_enable_refuses_before_doing_anything(self):
        def refused(*args, why, **canon):
            if canon:
                self.canon(**canon)
            res = self.dry("--enable", "deepseek6", *args)
            self.assertNotEqual(res.returncode, 0, args)
            self.assertIn(why, res.stderr, args)
            self.assertNotIn("+ ", res.stdout, args)     # nothing done, not even printed
            self.canon()
        refused(why="--enable needs --canon DIR")
        refused(*self.workspace_args(repo=False), why="--repo proj-a=https://")
        for url in ("https://x:tok@github.com/example-org/proj-a.git", "http://github.com/m/n",
                    "https://github.com/m/n?x=1", "git@github.com:m/n.git"):
            res = self.dry("--enable", "deepseek6", "--canon", self.canon(),
                           "--repo", "proj-a=%s#develop" % url)
            self.assertNotEqual(res.returncode, 0, url)
            self.assertNotIn("tok@", res.stdout)
        for branch in ("-x", "a..b", "a b"):
            res = self.dry("--enable", "deepseek6", "--canon", self.canon(),
                           "--repo", "proj-a=https://github.com/m/n#%s" % branch)
            self.assertNotEqual(res.returncode, 0, branch)
        args = self.workspace_args()
        refused(*args, why="deepseek6: not placed on vm-test", placed=("deepseek5",))
        refused(*args, why="not a directory of its own in /srv/ameesh-guest/work",
                work="/home/ameesh/work/{agent}")
        refused(*args, why="not a directory of its own in /srv/ameesh-guest/work",
                work="/srv/ameesh-guest/work/x/{agent}")
        refused(*args, why="deepseek6: no such agent", agents=("deepseek5",), placed=())
        res = self.dry("--enable", "deepseek6", "--canon", os.path.join(self.host, "etc"),
                       *args[2:])
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("the canon refuses", res.stderr)

    def test_enable_keeps_what_is_there(self):
        # bureau's activation: a clone (its URL without .git) and a profile
        self.on_host("/srv/ameesh-guest/work/deepseek6/.git/config",
                     '[remote "origin"]\n\turl = https://github.com/example-org/proj-a\n')
        self.on_host("/srv/ameesh-guest/work/deepseek6/.git/index", "DIRC")
        self.on_host("/srv/ameesh-guest/home/.dsh/hooks.json", "{}")
        self.on_host("/srv/ameesh-guest/home/.dsh/profiles/agent")
        res = self.dry("--enable", "deepseek6", *self.workspace_args(dsh=False))
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("kept /srv/ameesh-guest/work/deepseek6: a clone of "
                      "https://github.com/example-org/proj-a (never fetched nor reset here)", out)
        self.assertIn("kept /srv/ameesh-guest/home/.dsh: hooks.json and profiles there", out)
        self.assertNotIn("git -C /srv/ameesh-guest/work clone", out)
        self.assertNotIn("dsh_read tar", out)
        # the kept clone's helpers set all the same (the same helper)
        self.assertIn("git -C /srv/ameesh-guest/work/deepseek6 config --local --add "
                      "credential.helper /opt/ameesh/git-credential-guest", out)

    def test_enable_refuses_a_folder_holding_something_else(self):
        work = "/srv/ameesh-guest/work/deepseek6"
        self.on_host(work + "/.git/config", '[remote "origin"]\n\turl = https://github.com/x/y\n')
        res = self.dry("--enable", "deepseek6", *self.workspace_args())
        self.assertIn("is a clone of https://github.com/x/y, not of", res.stderr)
        subprocess.run(["rm", "-rf", self.host + work], check=True)
        self.on_host(work + "/notes.txt", "mine")
        res = self.dry("--enable", "deepseek6", *self.workspace_args())
        self.assertIn("is not empty and not a clone", res.stderr)
        os.remove(self.host + work + "/notes.txt")
        self.assertEqual(self.dry("--enable", "deepseek6", *self.workspace_args()).returncode, 0)
        if os.getuid() != 0:
            # an unreadable folder is never taken for an empty one
            os.chmod(self.host + work, 0)
            self.addCleanup(os.chmod, self.host + work, 0o700)
            res = self.dry("--enable", "deepseek6", *self.workspace_args())
            self.assertIn("is not empty and not a clone", res.stderr)
        subprocess.run(["rm", "-rf", self.host + work], check=True)
        self.on_host(work[:-1] + "x")
        os.symlink(self.host + work[:-1] + "x", self.host + work)
        res = self.dry("--enable", "deepseek6", *self.workspace_args())
        self.assertIn("is not empty and not a clone", res.stderr)

    def test_a_profile_holding_a_secret_is_not_copied(self):
        source = self.dsh(**{"profiles/agent/cordis.patch.yml":
                             "- llm:\n    apiKey: sk-%s\n" % ("abcdef" * 4)})
        args = self.workspace_args(dsh=False) + ["--dsh-from", source]
        res = self.dry("--enable", "deepseek6", *args)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("look like they hold a secret: profiles/agent/cordis.patch.yml", res.stderr)
        self.assertNotIn("abcdef", res.stdout + res.stderr)
        self.assertNotIn("+ ", res.stdout)
        res = self.dry("--enable", "deepseek6", *self.workspace_args(dsh=False))
        self.assertIn("give --dsh-from DIR", res.stderr)

    # --- codex #457 r1 ---

    def test_bash_x_never_traces_the_token(self):
        # P1: under bash -x, an assignment or an argument goes to stderr
        self.on_host("/etc/ameesh/guest-deepseek6.env",
                     "DEEPSEEK_API_KEY=k\nGH_TOKEN=%s\n" % self.TOKEN)
        res = subprocess.run(["bash", "-x", self.SCRIPT, "--dry-run", "--host", "vm-test",
                              "--enable", "deepseek6", *self.workspace_args()],
                             capture_output=True, text=True, env=self.env())
        self.assertEqual(res.returncode, 0, res.stderr[-2000:])
        self.assertIn("+ ", res.stderr)           # traced, all but the token
        self.assertNotIn(self.TOKEN, res.stdout + res.stderr)
        self.assertIn("ls-remote", res.stdout)     # the clone's path, all of it traced

    def clone_here(self, work="/srv/ameesh-guest/work/deepseek6"):
        """A real clone in the test's host: git init, its origin set."""
        path = self.host + work
        subprocess.run(["git", "init", "-q", path], check=True)
        subprocess.run(["git", "-C", path, "remote", "add", "origin",
                        "https://github.com/example-org/proj-a.git"], check=True)
        return path

    def helper_on_host(self, out):
        text = out.split("--- /opt/ameesh/git-credential-guest (0755 root:root)\n")[1]
        helper = self.on_host("/opt/ameesh/git-credential-guest", text.split("\n---\n")[0] + "\n")
        os.chmod(helper, 0o755)
        return helper

    def test_a_credential_store_never_gets_the_token(self):
        # codex #457 r2 P1: a credential.helper=store in the guest's global
        # config, after its own reset (through an include), or appended by
        # GIT_CONFIG_COUNT: the clone's .git/config (read after every file)
        # resets them, the command line too; nothing writes the token
        self.guest_user()
        self.on_host("/etc/ameesh/guest-deepseek6.env", "GH_TOKEN=%s\n" % self.TOKEN)
        fresh = self.dry("--enable", "deepseek6", *self.workspace_args()).stdout   # to clone
        clone = self.clone_here()
        self.on_host("/srv/ameesh-guest/work/deepseek6/.git/index", "DIRC")
        out = self.dry("--enable", "deepseek6", *self.workspace_args()).stdout
        helper = self.helper_on_host(out)
        home = self.on_host("/srv/ameesh-guest/home")
        store = os.path.join(home, ".git-credentials")
        self.on_host("/srv/ameesh-guest/home/store.inc", "[credential]\n\thelper = store --file %s\n" % store)
        self.on_host("/srv/ameesh-guest/home/.gitconfig",
                     "[credential]\n\thelper = \n[include]\n\tpath = %s/store.inc\n" % home)
        # the script's own `git … config --local` commands, on the clone
        mark = "+ runuser -u ameesh-guest -- git -C /srv/ameesh-guest/work/deepseek6 config "
        for line in out.splitlines():
            if line.startswith(mark):
                scope, op, key, value = line[len(mark):].split(" ", 3)
                subprocess.run(["git", "-C", clone, "config", scope, op, key, value], check=True)
        # --check reads the same list: ours alone after the last reset
        self.assertIn("ok   /srv/ameesh-guest/work/deepseek6: credential.helper "
                      "/opt/ameesh/git-credential-guest alone",
                      self.run_script("--check", "--host", "vm-test").stdout)
        # (git run here: the helper of the test's host)
        subprocess.run(["git", "-C", clone, "config", "--local", "--replace-all", "credential.helper",
                        helper, "^/opt/"], check=True)
        env = dict(os.environ, HOME=home, GH_TOKEN=self.TOKEN, GIT_TERMINAL_PROMPT="0",
                   GIT_CONFIG_NOSYSTEM="1")
        env.pop("GIT_CONFIG_GLOBAL", None)
        req = "protocol=https\nhost=github.com\npath=example-org/proj-a.git\n\n"
        def cred(action, data, **extra):
            return subprocess.run(["git", "-C", clone, "credential", action], input=data,
                                  capture_output=True, text=True, env=dict(env, **extra))
        got = cred("fill", req).stdout
        self.assertIn("password=%s" % self.TOKEN, got)
        cred("approve", got + "\n")
        self.assertFalse(os.path.exists(store))
        # the clone and ls-remote: the environment's config unset, every
        # file's helpers reset on the command line
        for cmd in ("ls-remote --exit-code", "clone --quiet"):
            line = next(l for l in fresh.splitlines() if cmd in l)
            self.assertIn("-- env -u GIT_CONFIG_COUNT -u GIT_CONFIG_PARAMETERS -u GH_TOKEN "
                          "GIT_TERMINAL_PROMPT=0 git -c credential.helper= -c credential.helper=!f()",
                          line)
        # that inline helper, run: the store set by the environment unset
        inline = self.inline_helper(fresh)
        for extra in ({}, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper",
                           "GIT_CONFIG_VALUE_0": "store --file " + store}):
            res = subprocess.run(["env", "-u", "GIT_CONFIG_COUNT", "git", "-c", "credential.helper=",
                                  "-c", "credential.helper=" + inline, "credential", "fill"],
                                 input=req, capture_output=True, text=True, env=dict(env, **extra))
            self.assertIn("password=%s" % self.TOKEN, res.stdout)
        # --check sees a store after the clone's reset (its own include)
        self.on_host("/srv/ameesh-guest/work/deepseek6/.git/store.inc",
                     "[credential]\n\thelper = store\n")
        subprocess.run(["git", "-C", clone, "config", "--local", "include.path", "store.inc"], check=True)
        out = self.run_script("--check", "--host", "vm-test").stdout
        self.assertIn("FAIL /srv/ameesh-guest/work/deepseek6: credential helpers in use: "
                      "%s, store, not" % helper, out)
        self.assertFalse(os.path.exists(store))

    def inline_helper(self, out):
        """The inline helper of --enable's ls-remote, reading the test's
        host's /etc/ameesh/guest-deepseek6.env."""
        line = next(l for l in out.splitlines() if "ls-remote --exit-code" in l)
        inline = line.split(" -c credential.helper=", 2)[2].split(" ls-remote ")[0]
        self.assertIn(" /etc/ameesh/guest-deepseek6.env ", inline)
        return inline.replace(" /etc/ameesh/", " %s/etc/ameesh/" % self.host)

    def test_the_token_is_in_no_environment(self):
        # codex #457 r2 P2: the token reaches no process's environment;
        # r3 P2: nor any file (a SIGKILL would leave it); r4 P2: nor a
        # descriptor git's children inherit (a hook): root hands git
        # nothing, the guest's helper reads the guest's own file
        with open(self.SCRIPT, encoding="utf-8") as fh:
            script = fh.read()
        self.assertNotRegex(script, r"GH_TOKEN=\$")
        self.assertNotIn("mktemp", script)
        self.assertNotRegex(script, r"<&3|3<&|3<")
        self.on_host("/etc/ameesh/guest-deepseek6.env", "GH_TOKEN=%s\n" % self.TOKEN)
        out = self.dry("--enable", "deepseek6", *self.workspace_args()).stdout
        self.assertNotRegex(out, r'GH_TOKEN=(?!//p")')    # (but the helper's sed)
        self.assertNotIn("/token", out)
        self.assertNotIn("token_of", out)
        self.assertEqual(out.count("+ runuser -u ameesh-guest -- env -u GIT_CONFIG_COUNT"), 2)
        self.assertNotIn(self.TOKEN, out)

    def test_the_inline_helper_asks_for_https_and_answers_again(self):
        # codex #457 r4 P2: protocol=http got the token; a second request
        # (a retry, a redirect) got nothing once fd 3 was read
        self.on_host("/etc/ameesh/guest-deepseek6.env",
                     "DEEPSEEK_API_KEY=k\nGH_TOKEN='%s'\n" % self.TOKEN)
        inline = self.inline_helper(self.dry("--enable", "deepseek6", *self.workspace_args()).stdout)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_NOSYSTEM="1")
        def fill(protocol="https", host="github.com"):
            return subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.helper=" + inline,
                                   "credential", "fill"],
                                  input="protocol=%s\nhost=%s\n\n" % (protocol, host),
                                  capture_output=True, text=True, env=env).stdout
        for _ in range(3):
            self.assertIn("username=x-access-token\npassword=%s\n" % self.TOKEN, fill())
        self.assertNotIn(self.TOKEN, fill("http"))
        self.assertNotIn(self.TOKEN, fill(host="evil.example"))
        self.assertNotIn(self.TOKEN, fill(host="github.com.evil.example"))

    def test_a_profile_swapped_while_read_is_refused(self):
        # codex #457 r2 P1 (TOCTOU): agent/ swapped for a link to elsewhere
        # between the check and the copy: what is copied is read again,
        # never through a link, and checked again: refused
        outside = self.on_host("/outside")
        with open(os.path.join(outside, "cordis.yml"), "w", encoding="utf-8") as fh:
            fh.write("shadow: root\n")
        with open(os.path.join(outside, "package.json"), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        source = self.dsh()
        agent = os.path.join(source, "profiles", "agent")
        swap = "mv %s %s.old && ln -s %s %s" % (agent, agent, outside, agent)
        res = self.dry("--enable", "deepseek6", *self.workspace_args(dsh=False), "--dsh-from", source,
                       PILOT_TEST_STAGE_HOOK=swap)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("profiles/agent: a link", res.stderr)
        self.assertNotIn("dsh_read tar", res.stdout)
        self.assertNotIn("systemctl enable", res.stdout)

    def test_a_linked_dsh_source_is_refused(self):
        # codex #457 r2 P1: --dsh-from itself a link, or through one
        source = self.dsh()
        link = os.path.join(self.host, "dsh-link")
        os.symlink(source, link)
        os.makedirs(os.path.join(self.host, "real", "home"))
        os.rename(source, os.path.join(self.host, "real", "home", "dsh"))
        os.symlink(os.path.join(self.host, "real", "home"), os.path.join(self.host, "home-link"))
        for given in (link, os.path.join(self.host, "home-link", "dsh")):
            res = self.dry("--enable", "deepseek6", *self.workspace_args(dsh=False), "--dsh-from", given)
            self.assertNotEqual(res.returncode, 0, given)
            self.assertIn("goes through a symbolic link", res.stderr)
            self.assertNotIn("+ ", res.stdout)

    def test_a_binary_or_unknown_file_is_not_copied(self):
        # codex #457 r2 P1: only the kinds a profile needs, text only: a DER
        # key renamed agent.txt, an id_* of any name, are refused
        der = "0\x82\x04\xbe\x02\x01\x000\r\x06\t*\x86H\x86\xf7\r\x01\x01\x01\x05\x00"
        for rel, data, why in (("profiles/agent/agent.txt", der.encode("latin-1"), "not text"),
                               ("profiles/agent/agent.txt", b"\xff\xfe\x00k", "not text"),
                               ("profiles/agent/id_work", b"x\n", "not a file a profile needs"),
                               ("profiles/agent/id_work.json", b"{}\n", "look like they hold a secret"),
                               ("profiles/agent/run.sh", b"echo\n", "not a file a profile needs")):
            source = self.dsh()
            with open(os.path.join(source, rel), "wb") as fh:
                fh.write(data)
            res = self.dry("--enable", "deepseek6", *self.workspace_args(dsh=False), "--dsh-from", source)
            self.assertNotEqual(res.returncode, 0, rel)
            self.assertIn(why, res.stderr, rel)
            self.assertIn(rel, res.stderr)
            self.assertNotIn("+ ", res.stdout)
            subprocess.run(["rm", "-rf", source], check=True)

    def test_links_on_the_destination_are_refused(self):
        # codex #457 r2 P1: hooks missing, the guest's .dsh/profiles/agent a
        # link to outside: refused before anything (and copied as the guest)
        outside = self.on_host("/outside")
        self.on_host("/srv/ameesh-guest/home/.dsh/profiles")
        os.symlink(outside, self.host + "/srv/ameesh-guest/home/.dsh/profiles/agent")
        res = self.dry("--enable", "deepseek6", *self.workspace_args())
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("/srv/ameesh-guest/home/.dsh/profiles/agent is a symbolic link", res.stderr)
        self.assertNotIn("+ ", res.stdout)
        os.remove(self.host + "/srv/ameesh-guest/home/.dsh/profiles/agent")
        os.rmdir(self.host + "/srv/ameesh-guest/home/.dsh/profiles")
        os.symlink(outside, self.host + "/srv/ameesh-guest/home/.dsh/profiles")
        res = self.dry("--enable", "deepseek6", *self.workspace_args())
        self.assertIn("/srv/ameesh-guest/home/.dsh/profiles is a symbolic link", res.stderr)

    def test_a_linked_profiles_folder_is_not_copied(self):
        # P1: --dsh-from DIR with DIR/profiles -> elsewhere
        source = self.dsh()
        elsewhere = os.path.join(self.host, "elsewhere")
        os.rename(os.path.join(source, "profiles"), elsewhere)
        os.symlink(elsewhere, os.path.join(source, "profiles"))
        res = self.dry("--enable", "deepseek6", *self.workspace_args(dsh=False),
                       "--dsh-from", source)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not copied:\n  profiles: a link", res.stderr)
        self.assertNotIn("+ ", res.stdout)

    def test_a_private_key_is_not_copied(self):
        # P1: PEM and OpenSSH private keys, by content or by name
        for extra in ({"profiles/agent/deploy": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blbn\n"},
                      {"profiles/agent/k.txt": "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n"},
                      {"profiles/agent/id_ed25519": "whatever\n"}):
            source = self.dsh(**extra)
            res = self.dry("--enable", "deepseek6", *self.workspace_args(dsh=False),
                           "--dsh-from", source)
            self.assertNotEqual(res.returncode, 0, extra)
            self.assertIn("not copied", res.stderr)
            self.assertIn(next(iter(extra)), res.stderr)
            self.assertNotIn("+ ", res.stdout)
            subprocess.run(["rm", "-rf", source], check=True)

    # --- codex #457 r3 ---

    def test_a_credential_setting_not_ours_is_refused(self):
        # P1: a helper for one URL (in config.worktree, the guest's global
        # file) is asked after the clone's reset; a credential.helper read
        # after .git/config (an include of it) too: a kept clone holding one
        # is refused before anything, and --check says it
        self.guest_user()
        self.on_host("/etc/ameesh/guest-deepseek6.env", "GH_TOKEN=%s\n" % self.TOKEN)
        clone = self.clone_here()
        self.on_host("/srv/ameesh-guest/work/deepseek6/.git/index", "DIRC")
        home = self.on_host("/srv/ameesh-guest/home")
        git = ["git", "-C", clone, "config"]
        url_helper = "credential.https://github.com.helper"
        subprocess.run([*git, "extensions.worktreeConfig", "true"], check=True)
        subprocess.run([*git, "--worktree", url_helper, "store"], check=True)
        def refused(why):
            res = self.dry("--enable", "deepseek6", *self.workspace_args())
            self.assertNotEqual(res.returncode, 0, why)
            self.assertIn("credential settings not ours, remove them first", res.stderr)
            self.assertIn(why, res.stderr)
            self.assertNotIn("+ ", res.stdout)
        refused("%s (worktree, file:%s/.git/config.worktree): not ours" % (url_helper, clone))
        subprocess.run([*git, "--worktree", "--unset", url_helper], check=True)
        gitconfig = os.path.join(home, ".gitconfig")
        subprocess.run(["git", "config", "--file", gitconfig, url_helper, "store"], check=True)
        refused("%s (global, file:%s): not ours" % (url_helper, gitconfig))
        # a generic helper in the global file: reset by the clone's own
        os.remove(gitconfig)
        subprocess.run(["git", "config", "--file", gitconfig, "credential.helper", "store"], check=True)
        self.on_host("/srv/ameesh-guest/work/deepseek6/.git/store.inc", "[credential]\n\thelper = store\n")
        subprocess.run([*git, "--local", "include.path", "store.inc"], check=True)
        refused("credential.helper store (local, file:%s/.git/store.inc): read after the clone's reset"
                % clone)
        subprocess.run([*git, "--local", "--unset", "include.path"], check=True)
        res = self.dry("--enable", "deepseek6", *self.workspace_args())
        self.assertEqual(res.returncode, 0, res.stderr)
        # --check: our helper alone in .git/config, a URL's in config.worktree
        out = res.stdout
        self.helper_on_host(out)
        for args in (("--replace-all", "credential.helper", ""),
                     ("--add", "credential.helper", "/opt/ameesh/git-credential-guest")):
            subprocess.run([*git, "--local", *args], check=True)
        check = lambda: self.run_script("--check", "--host", "vm-test").stdout
        self.assertIn("ok   /srv/ameesh-guest/work/deepseek6: credential.helper "
                      "/opt/ameesh/git-credential-guest alone", check())
        subprocess.run([*git, "--worktree", url_helper, "store"], check=True)
        self.assertIn("FAIL /srv/ameesh-guest/work/deepseek6: %s (worktree, file:%s/.git/config.worktree):"
                      " not ours" % (url_helper, clone), check())

    def test_the_source_swapped_above_it_is_refused(self):
        # P1 (TOCTOU): a directory above --dsh-from swapped for a link to
        # elsewhere once its path was checked: opened from / down, refused
        os.makedirs(os.path.join(self.host, "a", "b"))
        source = os.path.join(self.host, "a", "b", "dsh")
        os.rename(self.dsh(), source)
        elsewhere = os.path.join(self.host, "elsewhere")
        shutil.copytree(os.path.join(self.host, "a", "b"), elsewhere)
        with open(os.path.join(elsewhere, "dsh", "profiles", "agent", "cordis.yml"), "w",
                  encoding="utf-8") as fh:
            fh.write("shadow: root\n")
        b = os.path.join(self.host, "a", "b")
        swap = "mv %s %s.real && ln -s %s %s" % (b, b, elsewhere, b)
        res = self.dry("--enable", "deepseek6", *self.workspace_args(dsh=False), "--dsh-from", source,
                       PILOT_TEST_ROOT_HOOK=swap)
        self.assertTrue(os.path.islink(b))
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("%s: goes through a symbolic link" % source, res.stderr)
        self.assertNotIn("dsh_read tar", res.stdout)
        self.assertNotIn("systemctl enable", res.stdout)

    def test_a_fifo_never_blocks(self):
        # P2: a FIFO for hooks.json (opened before it was checked) blocked
        # root; read now without blocking, a plain file only. --check too.
        source = self.dsh()
        os.remove(os.path.join(source, "hooks.json"))
        os.mkfifo(os.path.join(source, "hooks.json"))
        res = subprocess.run(["bash", self.SCRIPT, "--dry-run", "--host", "vm-test", "--enable",
                              "deepseek6", *self.workspace_args(dsh=False), "--dsh-from", source],
                             capture_output=True, text=True, env=self.env(), timeout=60)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("hooks.json: not a plain file", res.stderr)
        self.assertNotIn("+ ", res.stdout)
        self.guest_user()
        self.on_host("/srv/ameesh-guest/home/.dsh/hooks.json", "{}")
        os.mkfifo(self.on_host("/srv/ameesh-guest/home/.dsh/profiles/agent") + "/cordis.yml")
        res = subprocess.run(["bash", self.SCRIPT, "--dry-run", "--check", "--host", "vm-test"],
                             capture_output=True, text=True, env=self.env(), timeout=60)
        self.assertIn("FAIL /srv/ameesh-guest/home/.dsh/profiles/agent/cordis.yml: not a file", res.stdout)

    def test_check_holds_the_profile_to_the_copy_rules(self):
        # P2: --check read the profile for secrets only: a DER key renamed
        # agent.txt passed. It is read now as --enable reads it.
        self.guest_user()
        self.on_host("/srv/ameesh-guest/home/.dsh/hooks.json", "{}")
        self.on_host("/srv/ameesh-guest/home/.dsh/profiles/agent/cordis.yml", "[]\n")
        check = lambda: self.run_script("--check", "--host", "vm-test").stdout
        self.assertNotIn("FAIL /srv/ameesh-guest/home/.dsh/", check())
        der = "0\x82\x04\xbe\x02\x01\x000\r\x06\t*\x86H\x86\xf7\r\x01\x01\x01\x05\x00"
        with open(self.host + "/srv/ameesh-guest/home/.dsh/profiles/agent/agent.txt", "wb") as fh:
            fh.write(der.encode("latin-1"))
        self.on_host("/srv/ameesh-guest/home/.dsh/profiles/agent/id_work", "x\n")
        out = check()
        self.assertIn("FAIL /srv/ameesh-guest/home/.dsh/profiles/agent/agent.txt: not text (UTF-8)", out)
        self.assertIn("FAIL /srv/ameesh-guest/home/.dsh/profiles/agent/id_work: not a file a profile "
                      "needs", out)

    def test_a_work_root_out_of_the_volume_is_refused(self):
        # P2: $ROOT/work a link to elsewhere: the clone would land there
        outside = self.on_host("/srv/outside")
        os.makedirs(self.host + "/srv/ameesh-guest", exist_ok=True)
        os.symlink(outside, self.host + "/srv/ameesh-guest/work")
        res = self.dry("--enable", "deepseek6", *self.workspace_args())
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("/srv/ameesh-guest/work goes through a symbolic link", res.stderr)
        self.assertNotIn("+ ", res.stdout)

    def test_a_missing_branch_is_found_before_anything(self):
        # P2: the branch asked first: absent, nothing is written ...
        self.on_host("/etc/ameesh/guest-deepseek6.env", "GH_TOKEN=%s\n" % self.TOKEN)
        res = self.dry("--enable", "deepseek6", *self.workspace_args(), PILOT_TEST_FAIL="runuser")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no branch develop", res.stderr)
        self.assertNotIn("--- /opt/ameesh", res.stdout)
        self.assertNotIn("clone", res.stdout)
        self.assertNotIn("systemctl", res.stdout)
        # ... and a clone never checked out is not kept on a rerun
        self.on_host("/srv/ameesh-guest/work/deepseek6/.git/config",
                     '[remote "origin"]\n\turl = https://github.com/example-org/proj-a.git\n')
        res = self.dry("--enable", "deepseek6", *self.workspace_args())
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("never checked out: move it away first", res.stderr)
        self.assertNotIn("systemctl enable", res.stdout)

    def test_hooks_already_there_are_never_overwritten(self):
        # P2: hooks.json there, profiles/ missing: only the profile copied
        self.on_host("/srv/ameesh-guest/home/.dsh/hooks.json", '{"mine": 1}')
        res = self.dry("--enable", "deepseek6", *self.workspace_args())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("kept /srv/ameesh-guest/home/.dsh/hooks.json: there already", res.stdout)
        tar = next(l for l in res.stdout.splitlines() if l.startswith("+ (dsh_read tar "))
        self.assertNotIn("hooks.json", tar)
        self.assertIn(" profiles/agent/cordis.yml profiles/agent/package.json) |", tar)

    def test_check_reports_the_workspace(self):
        self.guest_user()
        self.on_host("/etc/ameesh/guest-deepseek6.env", "GH_TOKEN=%s\n" % self.TOKEN)
        os.chmod(self.host + "/etc/ameesh/guest-deepseek6.env", 0o640)
        res = self.run_script("--check", "--host", "vm-test", "--canon", self.canon())
        out = res.stdout
        self.assertIn("FAIL /opt/ameesh/git-credential-guest missing", out)
        self.assertIn("FAIL /srv/ameesh-guest/home/.dsh: no hooks.json", out)
        self.assertIn("ok   GH_TOKEN set in guest-deepseek6.env", out)
        self.assertIn("FAIL /srv/ameesh-guest/work/deepseek6: not a clone", out)
        self.assertNotIn(self.TOKEN, out + res.stderr)
        # what --enable makes, made by hand in the test's host
        out = self.dry("--enable", "deepseek6", *self.workspace_args()).stdout
        text = out.split("--- /opt/ameesh/git-credential-guest (0755 root:root)\n")[1]
        os.chmod(self.on_host("/opt/ameesh/git-credential-guest",
                              text.split("\n---\n")[0] + "\n"), 0o755)
        self.on_host("/srv/ameesh-guest/home/.dsh/hooks.json", "{}")
        self.on_host("/srv/ameesh-guest/home/.dsh/profiles/agent/cordis.yml", "[]\n")
        clone = self.clone_here()
        out = self.run_script("--check", "--host", "vm-test", "--canon", self.canon()).stdout
        self.assertIn("FAIL /srv/ameesh-guest/work/deepseek6: credential helpers in use: none", out)
        for args in (("--replace-all", "credential.helper", ""),
                     ("--add", "credential.helper", "/opt/ameesh/git-credential-guest")):
            subprocess.run(["git", "-C", clone, "config", "--local", *args], check=True)
        self.on_host("/srv/ameesh-guest/work/deepseek6/.git/HEAD", "0123456789abcdef0123" * 2 + "\n")
        out = self.run_script("--check", "--host", "vm-test", "--canon", self.canon()).stdout
        self.assertIn("ok   /opt/ameesh/git-credential-guest (git credential helper, root 755)", out)
        self.assertIn("ok   /srv/ameesh-guest/work/deepseek6: credential.helper "
                      "/opt/ameesh/git-credential-guest alone", out)
        self.assertIn("ok   /srv/ameesh-guest/home/.dsh: hooks.json, profiles agent", out)
        self.assertIn("ok   /srv/ameesh-guest/work/deepseek6: a clone of "
                      "https://github.com/example-org/proj-a.git, detached at 0123456789ab", out)
        # without --canon: the folder taken for $ROOT/work/<agent>
        out = self.run_script("--check", "--host", "vm-test").stdout
        self.assertIn("no --canon: each work folder taken for /srv/ameesh-guest/work/<agent>", out)
        self.assertIn("ok   /srv/ameesh-guest/work/deepseek6: a clone of", out)
        # a helper changed, a profile with a secret: said
        with open(self.host + "/opt/ameesh/git-credential-guest", "a", encoding="utf-8") as fh:
            fh.write("echo leak\n")
        self.on_host("/srv/ameesh-guest/home/.dsh/profiles/agent/cordis.yml",
                     "token: %s\n" % self.TOKEN)
        out = self.run_script("--check", "--host", "vm-test").stdout
        self.assertIn("FAIL /opt/ameesh/git-credential-guest missing, changed", out)
        self.assertIn("FAIL /srv/ameesh-guest/home/.dsh/profiles/agent/cordis.yml looks like it "
                      "holds a secret", out)
        self.assertNotIn(self.TOKEN, out)

    def test_uninstall_removes_the_helper(self):
        res = self.dry("--uninstall")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("/opt/ameesh/git-credential-guest", res.stdout.split("+ rm -f /etc/tmpfiles.d")[1]
                      .splitlines()[0])

    def test_uninstall_follows_the_study_order(self):
        res = self.dry("--uninstall", "--purge")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        # runners and guard stop before their units are removed
        self.assertLess(out.index("disable --now ameesh-guard.service"),
                        out.index("rm -f /etc/systemd/system/ameesh-guest-runner@.service"))
        self.assertIn("rm -f /etc/tmpfiles.d/ameesh-gate.conf", out)
        self.assertIn("+ rm -f /etc/ameesh/guard.json\n", out)
        # secrets the script did not record as its own stay (codex #383 r2)
        self.assertNotIn("guest.env", out)
        # only what the script recorded as its own goes (none here)
        self.assertNotIn("userdel", out)
        self.assertNotIn("rm -rf /opt/ameesh/venv", out)

    def test_root_moves_the_runner(self):
        # codex #383 r1 P2: the runner's unit follows --root, all its paths
        res = self.dry("--allow-rootfs", "--root", "/mnt/guest")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertNotIn("/srv/ameesh-guest", out)
        for line in ("RequiresMountsFor=/mnt/guest", "WorkingDirectory=/mnt/guest",
                     "Environment=HOME=/mnt/guest/home", "Environment=TMPDIR=/mnt/guest/tmp",
                     "ReadWritePaths=/mnt/guest /run/ameesh-gate/ack"):
            self.assertIn(line + "\n", out)
        # disk_max: no volume, no limit
        self.assertIn("disk_max is indicative only", res.stderr)
        for root in ("/", "/home", "/srv/", "/srv/a b", "/srv/../etc", "/srv/x#y"):
            self.assertNotEqual(self.dry("--allow-rootfs", "--root", root).returncode, 0, root)

    def test_install_records_each_path_before_creating_it(self):
        # codex #383 r1 P1, P2: one record per directory the script creates,
        # written before it; a directory that is there is not recorded
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "guest")
            os.makedirs(os.path.join(root, "home"))
            res = self.dry("--allow-rootfs", "--root", root)
            self.assertEqual(res.returncode, 0, res.stderr)
            out = res.stdout
            self.assertNotIn("guest-dir %s'" % root, out)
            self.assertNotIn("guest-tree %s/home'" % root, out)
            for d in ("tmp", "work"):
                rec = out.index("record 'guest-tree %s/%s'" % (root, d))
                self.assertLess(rec, out.index("install -d -m 0700 -o ameesh-guest -g ameesh-guest %s/%s"
                                               % (root, d)))
            # the test's host has no /opt/ameesh/venv
            self.assertLess(out.index("record 'venv-tree /opt/ameesh/venv'"),
                            out.index("python3 -m venv /opt/ameesh/venv"))

    def test_purge_removes_only_the_recorded_paths(self):
        # codex #383 r1 P1: the absolute paths recorded, never /home, /tmp,
        # /work nor the --root of the command line; the last created first
        with tempfile.TemporaryDirectory() as tmp:
            made = os.path.join(tmp, "guest")
            for d in ("work", "tmp", "home"):
                os.makedirs(os.path.join(made, d))
            state = os.path.join(tmp, "pilot-install.state")
            with open(state, "w", encoding="utf-8") as fh:
                fh.write("user\nguest-dir %s\nguest-tree %s/work\nguest-tree /home\n"
                         "guest-tree /tmp\nguest-tree %s/gone\nroot-content\n"
                         "secret /etc/ameesh/guest-deepseek5.env\nsecret /etc/passwd\n"
                         % (made, made, made))
            res = self.run_script("--uninstall", "--purge", "--root", "/srv/elsewhere",
                                  PILOT_TEST_STATE=state)
            self.assertEqual(res.returncode, 0, res.stderr)
            out = res.stdout
            removed = [l for l in out.splitlines() if l.startswith("+ rm") and made in l]
            self.assertEqual(removed, ["+ rm -rf --one-file-system %s/work" % made,
                                           "+ rmdir --ignore-fail-on-non-empty %s" % made])
            self.assertIn("not removed: /home", out)
            self.assertIn("not removed: /tmp", out)
            for path in ("/home", "/tmp", "/srv/elsewhere", made + "/tmp", made + "/home"):
                self.assertNotIn("rm -rf --one-file-system %s\n" % path, out)
            self.assertIn("+ userdel ameesh-guest", out)
            # secrets: only those recorded, only the script's own files
            self.assertIn("+ rm -f /etc/ameesh/guest-deepseek5.env\n", out)
            self.assertNotIn("rm -f /etc/passwd", out)
            self.assertNotIn("guest-deepseek6", out)

    def test_an_existing_file_is_never_taken_for_a_directory(self):
        # codex #383 r2 P1: a file where a directory goes is refused before
        # any record (a purge would delete it)
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "guest")
            os.makedirs(root)
            with open(os.path.join(root, "work"), "w") as fh:
                fh.write("not ours")
            res = self.dry("--allow-rootfs", "--root", root)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("%s/work exists and is not a directory" % root, res.stderr)
            self.assertNotIn("record 'guest-tree", res.stdout)

    def test_mounts_beneath_a_tree_are_seen(self):
        # codex #383 r2 P1: a bind mount under a recorded tree stops its purge
        with open(self.SCRIPT, encoding="utf-8") as fh:
            text = fh.read()
        func = text[text.index("mounts_under() {"):]
        func = func[:func.index("\n}\n") + 3]
        def under(path):
            return subprocess.run(["bash", "-c", "MOUNTINFO=/proc/self/mountinfo\n" + func
                                   + 'mounts_under "$1"', "-", path],
                                  stdin=subprocess.DEVNULL, timeout=30).returncode
        self.assertEqual(under("/proc"), 0)          # /proc itself
        if os.path.ismount("/dev/shm"):
            self.assertEqual(under("/dev"), 0)       # /dev/shm beneath
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(under(tmp), 1)
        self.assertEqual(under("/pro"), 1)           # a prefix is not a parent

    def test_a_volume_bigger_than_disk_max_is_refused(self):
        # codex #383 r2 P2: nothing but the volume's size holds disk_max
        if not os.path.isdir("/dev/shm"):
            self.skipTest("no /dev/shm")
        lend = json.dumps({"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1MiB"})
        with tempfile.TemporaryDirectory() as tmp:
            info = self.mountinfo(tmp, "/dev/shm")
            res = self.dry("--root", "/dev/shm", "--lend", "/dev/stdin", stdin=lend,
                           PILOT_TEST_MOUNTINFO=info)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("refused for real", res.stderr)
            res = self.dry("--root", "/dev/shm", "--accept-larger-volume", "--lend",
                           "/dev/stdin", stdin=lend, PILOT_TEST_MOUNTINFO=info)
        self.assertNotIn("refused", res.stderr)
        self.assertIn("(accepted)", res.stderr)

    @staticmethod
    def mountinfo(tmp, *points):
        # a mountinfo of the test's own: "/" and the given mount points
        path = os.path.join(tmp, "mountinfo")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n")
            for i, point in enumerate(points):
                fh.write("%d 22 8:1 /data/x %s rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
                         % (40 + i, point))
        return path

    def test_a_root_through_a_symbolic_link_is_refused(self):
        # codex #383 r3 P1: mounts and records are compared as resolved paths
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            os.makedirs(os.path.join(tmp, "data"))
            os.symlink(os.path.join(tmp, "data"), os.path.join(tmp, "link"))
            res = self.dry("--allow-rootfs", "--root", os.path.join(tmp, "link", "guest"))
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("goes through a symbolic link: give %s/data/guest" % tmp, res.stderr)

    def test_purge_keeps_a_tree_with_a_bind_mount_beneath(self):
        # codex #383 r3 P1: a bind of the same filesystem, from mountinfo;
        # what stays keeps its record
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            work = os.path.join(tmp, "guest", "work")
            os.makedirs(os.path.join(work, "shared"))
            os.makedirs(os.path.join(tmp, "data"))
            os.symlink(os.path.join(tmp, "data"), os.path.join(tmp, "link"))
            linked = os.path.join(tmp, "link")
            state = os.path.join(tmp, "state")
            with open(state, "w", encoding="utf-8") as fh:
                fh.write("guest-tree %s\nguest-tree %s\n" % (work, linked))
            res = self.run_script("--uninstall", "--purge", PILOT_TEST_STATE=state,
                                  PILOT_TEST_MOUNTINFO=self.mountinfo(tmp, work + "/shared"))
            self.assertEqual(res.returncode, 0, res.stderr)
            out = res.stdout
            self.assertNotIn("rm -rf --one-file-system %s" % work, out)
            self.assertNotIn("rm -rf --one-file-system %s" % linked, out)
            self.assertIn("kept %s: a mount point is there or beneath" % work, out)
            self.assertIn("kept %s: it goes through a symbolic link" % linked, out)
            self.assertIn("kept in %s: guest-tree %s guest-tree %s" % (state, linked, work), out)
            self.assertNotIn("+ rm -f %s" % state, out)

    def test_a_broken_venv_is_never_removed_across_a_mount(self):
        # codex #383 r4 P1: the venv's recovery goes through the purge's own
        # protected removal (a bind of a user's home under share)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            venv = os.path.join(tmp, "opt", "venv")
            os.makedirs(os.path.join(venv, "share"))       # no bin/pip: broken
            state = os.path.join(tmp, "state")
            with open(state, "w", encoding="utf-8") as fh:
                fh.write("venv-tree %s\n" % venv)
            env = dict(PILOT_TEST_STATE=state, PILOT_TEST_VENV=venv,
                       PILOT_TEST_MOUNTINFO=self.mountinfo(tmp, venv + "/share"))
            res = self.dry("--allow-rootfs", "--root", os.path.join(tmp, "guest"), **env)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("kept %s: a mount point is there or beneath" % venv, res.stdout)
            self.assertIn("%s not made again" % venv, res.stderr)
            self.assertNotIn("rm -rf", res.stdout)
            # without the bind: removed, then made again
            env["PILOT_TEST_MOUNTINFO"] = self.mountinfo(tmp)
            res = self.dry("--allow-rootfs", "--root", os.path.join(tmp, "guest"), **env)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertLess(res.stdout.index("+ rm -rf --one-file-system %s\n" % venv),
                            res.stdout.index("+ python3 -m venv %s" % venv))

    def test_every_recursive_removal_is_the_protected_one(self):
        # codex #383 r4: one removal path for the whole script
        with open(self.SCRIPT, encoding="utf-8") as fh:
            lines = [l for l in fh if "rm -r" in l and not l.lstrip().startswith("#")]
        self.assertEqual([l.strip() for l in lines], ['run rm -rf --one-file-system "$path"'])

    def test_a_failed_userdel_keeps_its_record(self):
        # codex #383 r3 P3: the guest still has processes; a later purge retries
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            with open(state, "w", encoding="utf-8") as fh:
                fh.write("user\n")
            env = dict(PILOT_TEST_STATE=state, PILOT_TEST_FAIL="userdel")
            res = self.run_script("--uninstall", "--purge", **env)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("+ userdel ameesh-guest", res.stdout)
            self.assertIn("kept user ameesh-guest: userdel failed", res.stdout)
            self.assertIn("kept in %s: user" % state, res.stdout)
            self.assertNotIn("+ rm -f %s" % state, res.stdout)
            env.pop("PILOT_TEST_FAIL")
            res = self.run_script("--uninstall", "--purge", **env)
            self.assertIn("+ rm -f %s" % state, res.stdout)

    def test_a_bind_mounted_root_is_held_to_disk_max(self):
        # codex #383 r3 P2: a directory bind is a mount (mountinfo), and its
        # filesystem's size is held to disk_max
        lend = json.dumps({"cpu_max": 1, "memory_max": "1GiB", "disk_max": "1MiB"})
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            root = os.path.join(tmp, "pilot")
            os.makedirs(root)
            env = dict(PILOT_TEST_MOUNTINFO=self.mountinfo(tmp, root))
            run = lambda *a: self.dry("--root", root, "--lend", "/dev/stdin", *a,
                                      stdin=lend, **env)
            res = run()
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertNotIn("not a mount point", res.stdout)
            self.assertIn("refused for real", res.stderr)
            self.assertIn("(accepted)", run("--accept-larger-volume").stderr)
            # not in mountinfo: not a volume, whatever the directory
            env["PILOT_TEST_MOUNTINFO"] = self.mountinfo(tmp)
            res = run()
            self.assertIn("is not a mount point", res.stdout)
            self.assertIn("not a volume of its own", res.stderr)

    def test_observe_takes_the_caps_off(self):
        # codex #383 r1 P2: a stopped guard leaves its caps; observe removes them
        res = self.dry("--set-guard-mode", "observe")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        stop = out.index("+ systemctl stop ameesh-guard.service")
        reset = out.index("+ rm -rf --one-file-system /run/systemd/system.control/ameesh-guest.slice.d")
        start = out.index("+ systemctl start ameesh-guard.service")
        self.assertLess(stop, reset)
        self.assertLess(reset, start)

    def test_reinstall_restarts_the_guard(self):
        # codex #383 r1 P2: a new offer or mode reaches a running guard
        res = self.dry("--allow-rootfs", "--guard-mode", "enforce")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("AMEESH_GUARD_ARGS=\n", out)
        self.assertLess(out.index("+ systemctl stop ameesh-guard.service"),
                        out.index("+ systemctl enable --now ameesh-guard.service"))
        self.assertIn("the guard runs in enforce", out)

    def test_owner_runbook_commands_run(self):
        # PILOT-ameesh-agents-1.md: each of its script commands, dry run
        import re
        import shlex
        with open(os.path.join(GUEST, "PILOT-ameesh-agents-1.md"), encoding="utf-8") as fh:
            text = re.sub(r"\\\n\s*", "", fh.read())
        commands = []
        for line in text.splitlines():
            # (a comment starts at a "#" after a blank; --repo P=URL#BRANCH stays)
            m = re.search(r"sudo deploy/guest/install-pilot\.sh((?:[^`#|]|(?<=\S)#)*)", line)
            if m:
                commands.append(shlex.split(m.group(1)))
        self.assertGreaterEqual(len(commands), 8)
        enable = [a for a in commands if "--enable" in a]
        self.assertTrue(enable and all("--canon" in a and "--repo" in a for a in enable), enable)
        # the project is the runbook's own (--repo P=…): ameesh names none
        team = enable[0][enable[0].index("--repo") + 1].split("=", 1)[0]
        canon = self.canon(agents=("deepseek6", "deepseek1"), placed=("deepseek6", "deepseek1"),
                           team=team)
        dsh = self.dsh()
        for args in commands:
            args = [a for a in args if a != "--dry-run"]
            if "--host" not in args:
                args = ["--host", "vm-test", *args]   # --enable takes the rest
            # its canon and ~/.dsh: the test's own
            res = self.run_script(*args, PILOT_TEST_CANON=canon, PILOT_TEST_DSH=dsh)
            if "--check" in args:   # read only: it fails here, nothing being installed
                self.assertNotIn("install-pilot.sh:", res.stderr, args)
            else:
                self.assertEqual(res.returncode, 0, (args, res.stderr))


def reading(ts, **over):
    row = {"sampled_ts": float(ts), "mem_available_bytes": 8 * GIB, "swap_used_bytes": 0,
           "load1": 2.0, "cpu_count": 4, "disk_free_bytes": 100 * GIB,
           "turns_in_progress": 1, "guest_cpu": 0.5, "psi_cpu_some": 5.0,
           "psi_mem_some": 0.2, "psi_mem_full": 0.0, "lendable_cpu": 1.0,
           "lendable_memory_bytes": 2 * GIB}
    row.update(over)
    return row


class ReportTest(unittest.TestCase):
    def limits(self):
        return resources.thresholds(None)

    def test_percentile(self):
        self.assertIsNone(pilot.percentile([], 0.95))
        self.assertEqual(pilot.percentile([None, 3.0], 0.5), 3.0)
        self.assertEqual(pilot.percentile([1, 2, 3, 4, 5], 0.5), 3.0)
        self.assertAlmostEqual(pilot.percentile(range(101), 0.95), 95.0)

    def test_pressure_minutes_and_episodes(self):
        t0 = 1_800_000_000.0
        rows = [reading(t0 + 60 * i) for i in range(10)]
        # minutes 3 and 4: memory below half the floor (critical)
        for i in (3, 4):
            rows[i]["mem_available_bytes"] = 1
        # minute 7: memory below the floor only (blocked)
        rows[7]["mem_available_bytes"] = int(0.9 * GIB)
        rep = pilot.report("vm", rows, self.limits(), days=1)
        total = rep["total"]
        self.assertEqual(total["samples"], 10)
        self.assertEqual(total["measured_s"], 540.0)
        self.assertEqual(total["pressure_min"], 3.0)
        self.assertEqual(total["critical_episodes"], 1)
        self.assertEqual(total["turn_hours"], round(540 / 3600, 2))
        self.assertEqual(total["guest_cpu_hours"], round(0.5 * 540 / 3600, 2))
        self.assertEqual(total["lendable_memory_min"], 2 * GIB)

    def test_a_hole_in_the_measure_counts_for_nothing(self):
        t0 = 1_800_000_000.0
        rows = [reading(t0, mem_available_bytes=1),
                reading(t0 + 3600, mem_available_bytes=1),
                reading(t0 + 3660)]
        rep = pilot.report("vm", rows, self.limits(), days=1)
        self.assertEqual(rep["total"]["measured_s"], 60.0)
        self.assertEqual(rep["total"]["pressure_min"], 1.0)
        # two critical readings separated by a hole: two episodes
        self.assertEqual(rep["total"]["critical_episodes"], 2)

    def test_per_day_and_checks(self):
        t0 = 1_800_000_000.0
        rows = [reading(t0 + 60 * i, load1=10.0 if i == 5 else 2.0) for i in range(30)]
        rows += [reading(t0 + 86400 + 60 * i, tmp_fstype="tmpfs", tmp_size_bytes=100,
                         tmp_used_bytes=85) for i in range(30)]
        rep = pilot.report("vm", rows, self.limits(), days=2)
        self.assertEqual(rep["schema"], "ameesh-pilot-report/1")
        self.assertEqual(len(rep["by_day"]), 2)
        self.assertEqual(sum(d["samples"] for d in rep["by_day"]), 60)
        checks = {c["key"]: c for c in rep["checks"]}
        self.assertEqual(checks["load_p95"]["target"], 6.0)    # 1.5 × 4 CPU
        self.assertTrue(checks["load_p95"]["ok"])
        self.assertEqual(checks["tmpfs_max"]["value"], 0.85)
        self.assertFalse(checks["tmpfs_max"]["ok"])
        self.assertTrue(checks["psi_mem_p95"]["ok"])
        self.assertTrue(checks["critical_episodes"]["ok"])
        json.dumps(rep)

    def test_pressure_target_holds_each_day(self):
        # r1 P2: 15 min on one day and none on the next average 7.5 min/day,
        # yet the first day misses the target
        t0 = time.mktime((2027, 1, 15, 0, 0, 0, 0, 0, -1))
        rows = [reading(t0 + 300 * i) for i in range(2 * 288 + 1)]
        for i in (10, 11, 12):
            rows[i]["mem_available_bytes"] = int(0.9 * GIB)   # blocked, 5 min each
        rep = pilot.report("vm", rows, self.limits(), days=2)
        self.assertEqual([d["pressure_min"] for d in rep["by_day"]], [15.0, 0.0, 0.0])
        check = {c["key"]: c for c in rep["checks"]}["pressure_min_per_day"]
        self.assertEqual(check["value"], 15.0)
        self.assertFalse(check["ok"])
        rows[12]["mem_available_bytes"] = 8 * GIB
        check = {c["key"]: c for c in pilot.report("vm", rows, self.limits(),
                                                     days=2)["checks"]}["pressure_min_per_day"]
        self.assertEqual((check["value"], check["ok"]), (10.0, False))   # strictly under
        rows[11]["mem_available_bytes"] = 8 * GIB
        check = {c["key"]: c for c in pilot.report("vm", rows, self.limits(),
                                                     days=2)["checks"]}["pressure_min_per_day"]
        self.assertEqual((check["value"], check["ok"]), (5.0, True))

    def test_guest_cpu_stands_for_the_window_before_its_reading(self):
        # r1 P2: readings at 0/60/120 s, guest CPU None/0/60: the last one is
        # the average over 60–120 s, i.e. 3600 CPU-s (1 h), not over 120 s–
        t0 = 1_800_000_000.0
        rows = [reading(t0, guest_cpu=None), reading(t0 + 60, guest_cpu=0.0),
                reading(t0 + 120, guest_cpu=60.0)]
        self.assertEqual(pilot.report("vm", rows, self.limits(), days=1)
                         ["total"]["guest_cpu_hours"], 1.0)
        # a shorter measured window counts only for itself
        rows[2]["cpu_window_s"] = 30.0
        self.assertEqual(pilot.report("vm", rows, self.limits(), days=1)
                         ["total"]["guest_cpu_hours"], 0.5)
        # a window longer than the gap to the previous reading is capped by it
        rows[2]["cpu_window_s"] = 90.0
        self.assertEqual(pilot.report("vm", rows, self.limits(), days=1)
                         ["total"]["guest_cpu_hours"], 1.0)

    def test_an_interval_across_midnight_is_cut(self):
        # r1 P2: from 23:59 to 00:01, one minute on each day
        midnight = time.mktime((2027, 1, 16, 0, 0, 0, 0, 0, -1))
        blocked = int(0.9 * GIB)
        rows = [reading(midnight - 120, mem_available_bytes=blocked, guest_cpu=None),
                reading(midnight - 60, mem_available_bytes=blocked, guest_cpu=60.0),
                reading(midnight + 60, mem_available_bytes=blocked, guest_cpu=60.0)]
        rep = pilot.report("vm", rows, self.limits(), days=2)
        first, second = rep["by_day"]
        self.assertEqual((first["measured_s"], second["measured_s"]), (120.0, 60.0))
        self.assertEqual((first["pressure_min"], second["pressure_min"]), (2.0, 1.0))
        self.assertEqual((first["samples"], second["samples"]), (2, 1))
        # guest CPU: 60 s before 23:59 and 60 s before midnight on the first
        # day, the 60 s after it on the second
        self.assertEqual((first["guest_cpu_hours"], second["guest_cpu_hours"]), (2.0, 1.0))
        self.assertEqual(rep["total"]["measured_s"], 180.0)
        self.assertEqual(rep["total"]["guest_cpu_hours"], 3.0)

    def test_a_critical_episode_across_midnight_counts_once(self):
        # L159g2: the episode counts on the day it starts, not again after
        # midnight
        midnight = time.mktime((2027, 1, 16, 0, 0, 0, 0, 0, -1))
        rows = [reading(midnight + dt, mem_available_bytes=1) for dt in (-120, -60, 60)]
        rows.append(reading(midnight + 120))
        rep = pilot.report("vm", rows, self.limits(), days=2)
        self.assertEqual([d["critical_episodes"] for d in rep["by_day"]], [1, 0])
        self.assertEqual(rep["total"]["critical_episodes"], 1)

    def test_a_cpu_window_over_the_gap_limit_counts_for_nothing(self):
        # L159g2: readings 1 h apart, each the average of that hour: a hole
        # in the measure, not 300 s of guest CPU each
        t0 = 1_800_000_000.0
        rows = [reading(t0 + 3600 * i, guest_cpu=2.0, cpu_window_s=3600.0)
                for i in range(3)]
        total = pilot.report("vm", rows, self.limits(), days=1)["total"]
        self.assertEqual((total["measured_s"], total["guest_cpu_hours"]), (0.0, 0.0))
        # the same without a measured window: the gap to the previous reading
        for row in rows:
            del row["cpu_window_s"]
        self.assertEqual(pilot.report("vm", rows, self.limits(), days=1)
                         ["total"]["guest_cpu_hours"], 0.0)
        # readings 1 min apart, each averaged over the hour before it: the
        # measured window is judged before it is cut to the 60 s gap
        rows = [reading(t0 + 60 * i, guest_cpu=60.0, cpu_window_s=3600.0)
                for i in range(3)]
        total = pilot.report("vm", rows, self.limits(), days=1)["total"]
        self.assertEqual((total["measured_s"], total["guest_cpu_hours"]), (120.0, 0.0))

    def test_pressure_target_is_judged_unrounded(self):
        # L159g2: 599 s shows as 10.0 min but is under the 10 min target
        t0 = 1_800_000_000.0
        blocked = int(0.9 * GIB)
        rows = [reading(t0, mem_available_bytes=blocked),
                reading(t0 + 300, mem_available_bytes=blocked),
                reading(t0 + 599), reading(t0 + 660)]
        rep = pilot.report("vm", rows, self.limits(), days=1)
        self.assertEqual(rep["total"]["pressure_s"], 599.0)
        check = {c["key"]: c for c in rep["checks"]}["pressure_min_per_day"]
        self.assertEqual((check["value"], check["ok"]), (10.0, True))
        rows[2]["sampled_ts"] = t0 + 600
        check = {c["key"]: c for c in pilot.report("vm", rows, self.limits(),
                                                     days=1)["checks"]}["pressure_min_per_day"]
        self.assertEqual((check["value"], check["ok"]), (10.0, False))
        # 599.9999 s rounds to 600.0 in the summary, yet meets the target
        rows[2]["sampled_ts"] = t0 + 599.9999
        rep = pilot.report("vm", rows, self.limits(), days=1)
        self.assertEqual(rep["total"]["pressure_s"], 600.0)
        check = {c["key"]: c for c in rep["checks"]}["pressure_min_per_day"]
        self.assertEqual((check["value"], check["ok"]), (10.0, True))

    def test_no_reading(self):
        rep = pilot.report("vm", [], self.limits(), days=7)
        self.assertEqual(rep["total"]["samples"], 0)
        self.assertTrue(all(c["ok"] is None for c in rep["checks"]))
        self.assertIn("no reading", "\n".join(pilot.format_report(rep)))

    def test_format_report(self):
        t0 = 1_800_000_000.0
        rep = pilot.report("vm", [reading(t0 + 60 * i) for i in range(5)],
                           self.limits(), days=1)
        text = "\n".join(pilot.format_report(rep))
        self.assertIn("vm — pilot report", text)
        self.assertIn("total", text)
        self.assertIn("lendable", text)
        self.assertIn("pressure_min_per_day", text)


class ReportDbTest(PgTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.execute("DELETE FROM host_resources")

    def test_since_and_cli(self):
        hosts = storage.of(self.db).hosts
        for i in range(3):
            hosts.record({"host": "vm-pilot", "mem_available_bytes": 8 * GIB,
                          "load1": 1.0, "cpu_count": 4, "turns_in_progress": i})
        hosts.record({"host": "other", "mem_available_bytes": GIB})
        self.assertEqual(len(hosts.since("vm-pilot", 3600)), 3)
        proc = self.mesh("hosts", "vm-pilot", "--report", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rep = json.loads(proc.stdout)
        self.assertEqual(rep["host"], "vm-pilot")
        self.assertEqual(rep["total"]["samples"], 3)
        text = self.mesh("hosts", "vm-pilot", "--report", "--days", "1")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("pilot report", text.stdout)
        self.assertNotEqual(self.mesh("hosts", "--report").returncode, 0)
        self.assertNotEqual(self.mesh("hosts", "vm-pilot", "--report",
                                      "--days", "30").returncode, 0)


if __name__ == "__main__":
    unittest.main()
