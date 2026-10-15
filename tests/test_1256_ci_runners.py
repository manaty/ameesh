# SPDX-License-Identifier: AGPL-3.0-only
"""GitHub Actions runners of the ameesh guest on a lending host (lot #1256):
the runner unit, the single-use gate wrapper that keeps them behind the
guard's state, the installer's dry runs, the label separation of option A
(the borrowed runners never carry the fleet's label, and only a job outside
the required path may target them), and the isolation it promises to the
host's own application (never `actions.runner.*`, never `/home/runner`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from ameesh import canon, capacity_guard

from .support import REPO

GUEST = os.path.join(REPO, "deploy", "guest")
SCRIPT = os.path.join(GUEST, "ci-runners.sh")
UNIT = os.path.join(GUEST, "ameesh-guest-ci@.service")
PLAN = os.path.join(GUEST, "PILOT-ci-nexlink.md")
WRAPPER = os.path.join(GUEST, "ci-runner-gate.py")
SRC = os.path.join(REPO, "src")
# The privileged tools --prepare installs (the docker shim and the gate
# wrapper): root-owned, outside the guest's own root, which the guest owns
# (R1 delta B4, #1256, binôme).
TOOLS = "/usr/local/lib/ameesh-guest-ci"
TOOLS_REL = TOOLS.lstrip("/")
# the guest's own label, and the fleet's: a guest runner must never carry the
# second (option A of the R2 B1 arbitration)
LENT = "ameesh-lent"
FLEET = "ameesh-ci"


def structure_text(value):
    """Une structure chargée, rendue en texte. Les témoins ci-dessous n'y
    cherchent que des sous-chaînes ; les parts de la CI n'installent pas
    PyYAML (canon retombe sur son lecteur minimal), et `json` suffit à
    aplatir la structure de façon déterministe."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def jit_config(name="ameesh-nxl-ci-3-1", repo="https://github.com/manaty/ameesh",
               files=None, ephemeral=True):
    """A JIT configuration, as `POST …/generate-jitconfig` returns it: base64
    of a JSON object mapping runner file names to base64 contents
    (actions/runner v2.338.0, Runner.cs: every entry is written into the
    runner's root before the single job).

    `.runner` carries `ephemeral: true`: the one-shot path reads
    `settings.Ephemeral`, the JIT blob only writes files, so a configuration
    without it would leave a persistent runner registered (R1 Codex B1)."""
    if files is None:
        runnersettings = json.dumps(
            {"agentName": name, "gitHubUrl": repo, "ephemeral": ephemeral,
             "serverUrl": "https://pipelines.actions.githubusercontent.com/t/"})
        files = {".runner": runnersettings, ".credentials": "{}"}
    doc = {key: base64.b64encode(value.encode()).decode()
           for key, value in files.items()}
    return base64.b64encode(json.dumps(doc).encode()).decode()


def unit(name: str) -> dict[str, list[str]]:
    """{key: [values]} of a unit file (keys such as `Environment=` repeat)."""
    out: dict[str, list[str]] = {}
    with open(os.path.join(GUEST, name), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith(("#", "[")):
                continue
            key, _, value = line.partition("=")
            out.setdefault(key, []).append(value)
    return out


class GuestCiUnitTest(unittest.TestCase):
    def test_unit_is_single_use_and_stays_in_the_guest(self):
        u = unit("ameesh-guest-ci@.service")
        self.assertEqual(u["User"], ["ameesh-guest"])
        self.assertEqual(u["Slice"], ["ameesh-guest.slice"])
        self.assertEqual(u["Delegate"], ["yes"])  # rootless Podman's cgroups
        self.assertEqual(u["OOMScoreAdjust"], ["500"])
        self.assertEqual(u["TimeoutStopSec"], ["180"])
        self.assertEqual(u["RequiresMountsFor"], ["/srv/ameesh-guest"])
        env = dict(v.partition("=")[::2] for v in u["Environment"])
        self.assertTrue(env["PATH"].startswith(TOOLS + ":"))
        self.assertEqual(env["HOME"], "/srv/ameesh-guest/home")
        # the guest's own tree is not on the unit's PATH: a job cannot shadow
        # the privileged tools with a file of its own (R1 delta B4, binôme)
        self.assertNotIn("/srv/ameesh-guest", env["PATH"])
        # the gate (R1 #1256): the unit runs the wrapper, which obeys the
        # guard's state, not run.sh directly
        self.assertIn(TOOLS + "/ci-runner-gate", u["ExecStart"][0])
        self.assertIn("--dir /srv/ameesh-guest/ci/r%i", u["ExecStart"][0])
        # option A (#1256): one start consumes one deposited single-use
        # configuration, and nothing re-arms the unit behind the operator
        self.assertIn("--spool /srv/ameesh-guest/ci/jit/r%i", u["ExecStart"][0])
        self.assertEqual(u["Restart"], ["no"])
        self.assertEqual(env["AMEESH_HOST_GATE_PATH"],
                         "/run/ameesh-gate/state/state.json")
        self.assertNotIn("run.sh", u["ExecStart"][0])
        self.assertEqual(u["PrivateTmp"], ["yes"])
        # the unit is not the runner's own `svc.sh` one: no directive ever
        # enters the application's `actions.runner.*` namespace, and the
        # application's Docker daemon stays out of reach (comments may name
        # both, to say why)
        values = [v for vs in unit("ameesh-guest-ci@.service").values() for v in vs]
        for value in values:
            self.assertNotIn("actions.runner", value)
            self.assertNotIn("/run/docker.sock", value)
        # the strict profile of the agent unit would break newuidmap/fuse
        for key in ("NoNewPrivileges", "PrivateDevices", "ProtectSystem"):
            self.assertNotIn(key, u)


def gate_doc(state="available", mode="enforce", *, ttl=90.0, seq=1):
    """A guard's state document (`ameesh-host-state/1`, L159f)."""
    return {"schema": "ameesh-host-state/1", "state": state, "seq": seq,
            "expires_ts": time.time() + ttl, "guard": {"mode": mode}}


class GateWrapperTest(unittest.TestCase):
    """deploy/guest/ci-runner-gate.py: the guest's runner (a fake run.sh here)
    starts **one** single-use configuration, only while the guard publishes a
    fresh `available` out of `enforce`, and is stopped when the state closes,
    disappears or goes stale (R1 #1256, option A)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.realpath(tmp.name)
        self.run_dir = os.path.join(self.dir, "r1")
        self.spool = os.path.join(self.dir, "jit/r1")
        os.makedirs(self.run_dir)
        os.makedirs(self.spool)
        self.events = os.path.join(self.dir, "events")
        self.gate = os.path.join(self.dir, "state.json")
        self.done = os.path.join(self.dir, "done")
        self.write(os.path.join(self.run_dir, "run.sh"),
                   "#!/bin/sh\n"
                   "# the real runner writes these from the JIT blob, for the\n"
                   "# length of one job (Runner.cs: RunJitConfig)\n"
                   "echo '{\"agentName\":\"ameesh-nxl-ci-3-1\"}' > .runner\n"
                   "echo '{}' > .credentials\n"
                   "echo \"start $*\" >> %s\n"
                   "trap 'echo term >> %s; exit 0' TERM\n"
                   "while [ ! -e %s ]; do sleep 0.1; done\n"
                   "echo done >> %s\n"
                   "exit 0\n" % (self.events, self.events, self.done, self.events),
                   mode=0o755)
        self.procs = []

    def write(self, path, content, mode=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        if mode is not None:
            os.chmod(path, mode)
        return path

    def deposit(self, blob=None, name="jit-0001.b64"):
        path = os.path.join(self.spool, name)
        self.write(path, (blob if blob is not None else jit_config()) + "\n", mode=0o600)
        return path

    def set_gate(self, **kwargs):
        self.write(self.gate, json.dumps(gate_doc(**kwargs)))

    def start(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [SRC] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        proc = subprocess.Popen(
            [sys.executable, WRAPPER, "--dir", self.run_dir, "--spool", self.spool,
             "--gate", self.gate, "--interval", "0.1", "--grace", "3"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        self.procs.append(proc)
        self.addCleanup(self.stop, proc)
        return proc

    def stop(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()

    def events_now(self):
        try:
            with open(self.events, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def wait_event(self, text, timeout=6.0):
        return self.wait_count(text, 1, timeout=timeout)

    def wait_count(self, text, count, timeout=6.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.events_now().count(text) >= count:
                return True
            time.sleep(0.05)
        return False

    def runner_alive(self):
        res = subprocess.run(["ps", "-eo", "args="], capture_output=True, text=True)
        return any(self.run_dir + "/run.sh" in line for line in res.stdout.splitlines())

    def test_a_closed_gate_consumes_nothing(self):
        # no document, then an observing guard: a lending host is closed by
        # default, and a closed gate must not burn a single-use configuration
        path = self.deposit()
        # a stale local configuration from a killed run: it is removed before
        # the next job starts (a credential that outlived its job is dead)
        self.write(os.path.join(self.run_dir, ".runner"), "stale\n")
        proc = self.start()
        time.sleep(0.5)
        self.set_gate(mode="observe")
        time.sleep(0.5)
        self.assertNotIn("start", self.events_now())
        self.assertTrue(os.path.exists(path), "a closed gate consumed the configuration")
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, ".runner")))
        self.assertFalse(self.runner_alive())
        # available out of enforce: it starts, and consumes exactly that one
        self.set_gate()
        self.assertTrue(self.wait_event("start"), self.events_now())
        self.assertFalse(os.path.exists(path))
        self.assertIn("--jitconfig", self.events_now())
        self.assertTrue(self.runner_alive())
        proc.terminate()
        proc.wait(timeout=10)

    def test_one_start_consumes_one_configuration_and_cleans_up(self):
        # the runner writes `.runner`/`.credentials` from the JIT blob; they
        # must not outlive the job (option A: no durable credential here)
        self.deposit()
        self.set_gate()
        proc = self.start()
        self.assertTrue(self.wait_event("start"), self.events_now())
        self.assertTrue(os.path.exists(os.path.join(self.run_dir, ".runner")),
                        "the runner's own JIT files must exist during the job")
        # a second start with an empty spool consumes nothing and says so
        second = self.start()
        self.assertEqual(second.wait(timeout=10), 0)
        self.assertIn("no configuration deposited", second.stdout.read())
        self.assertEqual(self.events_now().count("start"), 1)
        # the job ends normally: the configuration is spent, the local files
        # the runner wrote are removed, and the wrapper returns the exit code
        self.write(self.done, "")
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertTrue(self.wait_event("done"), self.events_now())
        for leftover in (".runner", ".credentials", ".credentials_rsaparams"):
            self.assertFalse(os.path.exists(os.path.join(self.run_dir, leftover)),
                             "%s outlived the job" % leftover)
        self.assertEqual(len(os.listdir(self.spool)), 0)

    def test_the_configuration_is_handed_to_run_sh_exactly_once(self):
        blob = jit_config()
        self.deposit(blob)
        self.set_gate()
        self.start()
        self.assertTrue(self.wait_event("start"), self.events_now())
        events = self.events_now()
        self.assertEqual(events.count("start"), 1)
        self.assertIn("--jitconfig %s" % blob, events)

    def test_a_closed_state_stops_the_single_use_runner(self):
        # the guard died or hung: its state expires, so the guest stops itself
        self.deposit()
        self.set_gate()
        self.start()
        self.assertTrue(self.wait_event("start"), self.events_now())
        self.write(self.gate, json.dumps(gate_doc(ttl=-1.0)))
        self.assertTrue(self.wait_event("term"), self.events_now())
        self.assertFalse(self.runner_alive())

    def test_an_invalid_configuration_is_refused_not_run(self):
        self.deposit(blob="not base64 at all", name="jit-bad.b64")
        self.set_gate()
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 78)
        out = proc.stdout.read()
        self.assertIn("refusing to run it", out)
        self.assertNotIn("start", self.events_now())
        self.assertTrue(os.path.exists(os.path.join(self.spool, "jit-bad.b64")))

    def test_a_persistent_configuration_is_refused_not_run(self):
        # sans `ephemeral: true`, le runner resterait enregistré après le job
        # (Runner.cs, `settings.Ephemeral`) : le portier refuse, ne consomme
        # pas et ne démarre rien (R1 Codex B1)
        persistent = jit_config(files={
            ".runner": json.dumps({"agentName": "ameesh-nxl-ci-3-1",
                                   "gitHubUrl": "https://github.com/manaty/ameesh",
                                   "ephemeral": False}),
            ".credentials": "{}"})
        path = self.deposit(persistent)
        self.set_gate()
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 78)
        self.assertIn("ephemeral true", proc.stdout.read())
        self.assertNotIn("start", self.events_now())
        self.assertTrue(os.path.exists(path))

    def test_a_configuration_that_escapes_the_runner_root_is_refused(self):
        # `run.sh --jitconfig` écrit chaque entrée dans la racine du runner :
        # un nom de fichier qui remonte l'arbre est refusé par le portier,
        # comme par le dépôt (R1 Codex B2)
        blob = jit_config(files={"../../outside": "eA==", ".runner": json.dumps(
            {"agentName": "ameesh-nxl-ci-3-1", "ephemeral": True})})
        path = self.deposit(blob)
        self.set_gate()
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 78)
        self.assertIn("unsafe runner file name", proc.stdout.read())
        self.assertNotIn("start", self.events_now())
        self.assertTrue(os.path.exists(path))

    def test_an_oversized_configuration_is_refused_whole(self):
        # la borne s'applique à ce qui est lu, sur le descripteur : un fichier
        # trop grand n'est jamais tronqué en configuration qui semblerait
        # valide (R1 Codex B2)
        path = self.write(os.path.join(self.spool, "jit-big.b64"),
                          "a" * (65536 + 1), mode=0o600)
        self.set_gate()
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 78)
        self.assertIn("larger than", proc.stdout.read())
        self.assertNotIn("start", self.events_now())
        self.assertTrue(os.path.exists(path))

    @unittest.skipIf(os.geteuid() == 0, "root ignores the directory mode")
    def test_a_configuration_that_cannot_be_consumed_starts_nothing(self):
        # consommer d'abord : si l'unlink échoue, le portier refuse au lieu de
        # lancer un blob qui resterait déposé et pourrait courir deux fois
        path = self.deposit()
        os.chmod(self.spool, 0o555)
        self.addCleanup(os.chmod, self.spool, 0o755)
        self.set_gate()
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 78)
        self.assertIn("cannot be consumed", proc.stdout.read())
        self.assertNotIn("start", self.events_now())
        self.assertTrue(os.path.exists(path))

    def test_a_fifo_configuration_cannot_block_a_valid_deposit(self):
        fifo = os.path.join(self.spool, "jit-0000.b64")
        os.mkfifo(fifo)
        valid = self.deposit()
        self.write(self.done, "done")
        self.set_gate()
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual(self.events_now().count("start"), 1)
        self.assertFalse(os.path.exists(valid))
        self.assertTrue(os.path.exists(fifo))

    def test_a_symlinked_configuration_is_left_alone(self):
        # the spool belongs to the guest; a link planted there is never a
        # configuration (O_NOFOLLOW), and it does not block a real one
        real = self.write(os.path.join(self.dir, "elsewhere.b64"),
                          jit_config() + "\n", mode=0o600)
        os.symlink(real, os.path.join(self.spool, "jit-link.b64"))
        self.set_gate()
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertIn("no configuration deposited", proc.stdout.read())
        self.assertNotIn("start", self.events_now())
        self.assertTrue(os.path.exists(real))

    def test_a_missing_runner_is_a_configuration_refusal(self):
        os.remove(os.path.join(self.run_dir, "run.sh"))
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 78)
        self.assertIn("missing or not executable", proc.stdout.read())

    def test_a_missing_spool_is_a_configuration_refusal(self):
        shutil.rmtree(self.spool)
        proc = self.start()
        self.assertEqual(proc.wait(timeout=10), 78)
        self.assertIn("is not a directory", proc.stdout.read())


class CiRunnersTest(unittest.TestCase):
    """deploy/guest/ci-runners.sh: dry runs only, against a host of its own
    (CI_RUNNERS_TEST_HOST): never the real /etc/ameesh, the real systemd, the
    real users, nor anything else of the machine this test runs on."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.host = os.path.realpath(tmp.name)
        for d in ("etc/systemd/system", "etc/ameesh", "srv/ameesh-guest",
                  TOOLS_REL, "root", "bin"):
            os.makedirs(os.path.join(self.host, d))
        self.write("etc/passwd",
                   "root:x:0:0:root:/root:/bin/bash\n"
                   "ameesh-guest:x:998:998::/srv/ameesh-guest/home:/bin/bash\n")
        self.write("etc/group", "root:x:0:\nameesh-guest:x:998:\n")
        self.write("etc/systemd/system/ameesh-guest.slice", "[Slice]\nCPUWeight=10\n")
        self.write("etc/ameesh/guard.json", json.dumps(
            {"host": "ci-3", "max_agents": 0, "agent_units": "ameesh-guest-*.service",
             "lend": {"cpu_max": 2, "memory_max": "6GiB", "disk_max": "30GiB"}}))
        self.write(TOOLS_REL + "/docker", "#!/bin/sh\n# lot #1256\nexit 0\n", mode=0o755)
        # the tools the preconditions look for, without this machine's
        for name in ("podman", "psql", "git"):
            self.write("bin/%s" % name, "#!/bin/sh\nexit 0\n", mode=0o755)
        # a real (non-dry) run pointed at this host substitutes this shim for
        # `runuser`: the guest commands run as the test user (TEST_SELF), so
        # the deposit's publication path is exercised without root (R2 cx2
        # #1256)
        self.write("bin/runuser",
                   "#!/bin/sh\n"
                   "[ \"$1\" = -u ] && shift 2\n"
                   "[ \"$1\" = -- ] && shift\n"
                   "exec \"$@\"\n", mode=0o755)

    def real_run(self, *args, **env_extra):
        """A real run (no `--dry-run`) against the test host: non-root, the
        `runuser` shim on PATH; refuses to touch anything else (R2 cx2 #1256)."""
        return subprocess.run(["bash", SCRIPT, *args], capture_output=True, text=True,
                              env=self.script_env(**env_extra))

    def write(self, path, content, mode=None):
        full = os.path.join(self.host, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        if mode is not None:
            os.chmod(full, mode)
        return full

    def script_env(self, **env_extra):
        env = dict(os.environ, CI_RUNNERS_TEST_HOST=self.host,
                   PATH=os.path.join(self.host, "bin") + os.pathsep + os.environ["PATH"])
        env.update({name: str(value) for name, value in env_extra.items()})
        return env

    def run_script(self, *args, busy=False, **env_extra):
        if busy:
            env_extra["CI_RUNNERS_TEST_BUSY"] = "1"
        return subprocess.run(["bash", SCRIPT, "--dry-run", *args],
                              capture_output=True, text=True,
                              env=self.script_env(**env_extra))

    def start_script(self, *args, **env_extra):
        """Un check qui vit pendant le témoin : lu sur stdout (octets, sans
        tampon Python), arrêté en cleanup même si une assertion échoue avant
        sa fin."""
        proc = subprocess.Popen(["bash", SCRIPT, "--dry-run", *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=self.script_env(**env_extra))
        self.addCleanup(self.stop_script, proc)
        return proc

    def stop_script(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    def sync_on_stdout(self, proc, needle, timeout=30.0):
        """Lit stdout jusqu'à `needle` : les `say` du script sortent aussitôt
        (`echo`), donc le témoin sait que la fonction observée est entrée
        avant de muter marqueur ou porte — aucune durée fixe ne décide de
        l'ordre (canon §3)."""
        deadline = time.monotonic() + timeout
        seen = b""
        while needle not in seen:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([proc.stdout], [], [], left)[0]:
                break
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            seen += chunk
        self.assertIn(needle, seen, seen.decode(errors="replace"))
        return seen

    def finish_script(self, proc, seen=b"", timeout=30):
        out = seen + (proc.stdout.read() or b"")
        err = proc.stderr.read() or b""
        proc.wait(timeout=timeout)
        return out.decode(), err.decode()

    def prepared(self, n, name=None):
        """The runner slot is installed (config.sh) — option A has no
        `.runner` until a JIT configuration runs one job."""
        self.write("srv/ameesh-guest/ci/r%d/config.sh" % n, "#!/bin/sh\nexit 0\n",
                   mode=0o755)

    def deposit_file(self, n=1, name=None, blob=None, repo="https://github.com/manaty/ameesh"):
        name = name or ("ameesh-nxl-ci-3-%d" % n)
        return self.write("root/jit-r%d.b64" % n,
                          (blob if blob is not None else jit_config(name=name, repo=repo))
                          + "\n", mode=0o600)

    def arm(self, n=1, blob=None):
        """A configuration physically deposited in the slot's spool: what
        `--deposit` writes outside a dry run (the dry runs cannot)."""
        blob = blob if blob is not None else jit_config(name="ameesh-nxl-ci-3-%d" % n)
        canonical = "".join(blob.split())
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        self.prepared(n)
        return self.write("srv/ameesh-guest/ci/jit/r%d/jit-%s.b64" % (n, digest),
                          canonical, mode=0o600)

    def pending(self, n=1):
        root = os.path.join(self.host, "srv/ameesh-guest/ci/jit/r%d" % n)
        return [f for f in os.listdir(root) if f.endswith(".b64")]

    def test_prepare_prints_the_install_and_changes_nothing(self):
        before = sorted(os.path.relpath(os.path.join(d, f), self.host)
                        for d, _, files in os.walk(self.host) for f in files)
        res = self.run_script("--prepare", "--host", "ci-3", "--count", "2")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("actions-runner-linux-x64-2.338.0.tar.gz", out)
        self.assertIn("af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32", out)
        self.assertIn("ci/r1", out)
        self.assertIn("ci/r2", out)
        self.assertIn("ci/jit/r1", out)          # the single-use deposit directory
        self.assertIn("DRY write /etc/systemd/system/ameesh-guest-ci@.service", out)
        self.assertIn("systemctl daemon-reload", out)
        # nothing it would run ever names the application's namespace or trees
        for line in out.splitlines():
            if line.startswith("DRY ") or line.startswith("next,"):
                self.assertNotIn("/home/runner", line)
                self.assertNotIn("actions.runner", line)
        after = sorted(os.path.relpath(os.path.join(d, f), self.host)
                       for d, _, files in os.walk(self.host) for f in files)
        self.assertEqual(before, after)

    def test_the_shim_is_rootless_podman_not_the_host_daemon(self):
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("exec podman --cgroup-manager=cgroupfs", res.stdout)
        self.assertNotIn("DOCKER_HOST", res.stdout)

    def test_prepare_refuses_without_the_pilot(self):
        os.remove(os.path.join(self.host, "etc/systemd/system/ameesh-guest.slice"))
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("install-pilot.sh first", res.stderr)

    def test_prepare_refuses_a_root_equivalent_guest(self):
        self.write("etc/group", "root:x:0:\ndocker:x:999:ameesh-guest\n")
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("root-equivalent", res.stderr)

    def test_prepare_refuses_a_foreign_shim(self):
        self.write(TOOLS_REL + "/docker", "#!/bin/sh\nexit 0\n", mode=0o755)
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not this script's shim", res.stderr)

    def test_prepare_refuses_a_foreign_unit(self):
        self.write("etc/systemd/system/ameesh-guest-ci@.service", "# another unit\n", mode=0o644)
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("differs from this checkout", res.stderr)

    def test_the_guard_must_cover_the_ci_units(self):
        self.write("etc/ameesh/guard.json", json.dumps(
            {"host": "ci-3", "agent_units": "ameesh-guest-runner@*.service"}))
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--agent-units", res.stderr)

    def test_the_disk_floor_is_qualified_per_host(self):
        # #1256 : le plancher fixe de 15 GiB refuse la ci-3 mesurée (8.95 GiB
        # libres). Il est donc qualifié par l'opérateur (--disk-floor-mib),
        # jamais contourné en silence : sous le plancher demandé, --prepare
        # refuse, et la valeur est validée.
        res = self.run_script("--prepare", "--host", "ci-3",
                              "--disk-floor-mib", "999999")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("999999 MiB floor", res.stderr)
        self.assertIn("--disk-floor-mib", res.stderr)
        res = self.run_script("--prepare", "--host", "ci-3", "--disk-floor-mib", "1")
        self.assertEqual(res.returncode, 0, res.stderr)
        res = self.run_script("--prepare", "--host", "ci-3", "--disk-floor-mib", "0")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("positive", res.stderr)
        res = self.run_script("--prepare", "--host", "ci-3", "--disk-floor-mib", "12x")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("invalid --disk-floor-mib", res.stderr)
        # au-delà de 1 TiB, l'arithmétique déborderait : refusé, pas replié
        res = self.run_script("--prepare", "--host", "ci-3",
                              "--disk-floor-mib", "999999999999999999999")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("1048576 MiB", res.stderr)

    def test_there_is_no_persistent_registration_any_more(self):
        # option A (arbitrage R2 B1) : plus de jeton d'enregistrement sur
        # l'hôte, plus de `config.sh --unattended`, plus de label de la
        # flotte. Les deux anciennes options refusent en nommant le dépôt.
        self.write("root/reg.token", "ghs_" + "x" * 32 + "\n", mode=0o600)
        res = self.run_script("--register", "--token-file", "/root/reg.token")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--deposit", res.stderr)
        self.assertIn("single-use", res.stderr)
        res = self.run_script("--prepare", "--token-file", "/root/reg.token")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--deposit", res.stderr)
        with open(SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("--labels " + FLEET, src)   # jamais le label de la flotte
        self.assertNotIn("--unattended", src)
        self.assertIn("LABEL=" + LENT, src)

    @unittest.skipIf(os.geteuid() == 0, "the real-deposit seam is a non-root run")
    def test_a_real_deposit_publishes_the_validated_bytes(self):
        """Vrai dépôt (pas de `--dry-run`) : le fichier publié est un lien vers
        le temporaire vérifié, aux octets validés, propriété de l'invité, et
        aucun temporaire ne survit (R2 cx2 B2, #1256)."""
        self.write("srv/ameesh-guest/ci/jit/r1/.keep", "")
        source = self.deposit_file(1)
        res = self.real_run("--deposit", "/root/jit-r1.b64", "--host", "ci-3")
        self.assertEqual(res.returncode, 0, res.stderr)
        with open(source, "rb") as fh:
            raw = fh.read()
        digest = hashlib.sha256(b"".join(raw.split())).hexdigest()[:16]
        spool = os.path.join(self.host, "srv/ameesh-guest/ci/jit/r1")
        target = os.path.join(spool, "jit-%s.b64" % digest)
        self.assertTrue(os.path.isfile(target), os.listdir(spool))
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), raw)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
        self.assertEqual(sorted(os.listdir(spool)), [".keep", os.path.basename(target)])

    @unittest.skipIf(os.geteuid() == 0, "the real-deposit seam is a non-root run")
    def test_a_real_deposit_refuses_a_source_that_changed(self):
        """Le shim remplace A par B après validation, avant la copie invitée,
        sans dépendre d'un délai. Aucun lien ne doit être tenté : le seul
        spool final vide ne détectait pas publier-puis-retirer (B2, #1256)."""
        self.deposit_file(1)
        self.write("root/other.b64", jit_config(repo="https://github.com/other/repo"))
        self.write("bin/runuser", "#!/bin/sh\n"
                   "[ \"$1\" = -u ] && shift 2\n"
                   "[ \"$1\" = -- ] && shift\n"
                   # The shell has opened stdin already: overwrite the same
                   # inode before the guest command reads from that descriptor.
                   'cat "$CI_RUNNERS_TEST_HOST/root/other.b64" '
                   '> "$CI_RUNNERS_TEST_HOST/root/jit-r1.b64"\n'
                   'exec "$@"\n', mode=0o755)
        real_ln = shutil.which("ln")
        self.assertIsNotNone(real_ln)
        self.write("bin/ln", "#!/bin/sh\n"
                   ': > "$CI_RUNNERS_TEST_HOST/link-attempted"\n'
                   'exec "$CI_RUNNERS_TEST_LN" "$@"\n', mode=0o755)
        self.write("srv/ameesh-guest/ci/jit/r1/.keep", "")
        res = self.real_run("--deposit", "/root/jit-r1.b64", "--host", "ci-3",
                            CI_RUNNERS_TEST_LN=real_ln)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("changed while being deposited", res.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.host, "link-attempted")))
        spool = os.path.join(self.host, "srv/ameesh-guest/ci/jit/r1")
        self.assertEqual(sorted(os.listdir(spool)), [".keep"])

    def test_deposit_prints_one_configuration_and_never_its_bytes(self):
        blob = jit_config()
        self.prepared(1)
        self.write("srv/ameesh-guest/ci/jit/r1/.keep", "")
        self.deposit_file(1, blob=blob)
        res = self.run_script("--deposit", "/root/jit-r1.b64", "--host", "ci-3")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        # root ne lit que la source, l'invité écrit sous son arbre (B4)
        self.assertIn("DRY runuser -u ameesh-guest -- sh -c", out)
        self.assertIn("srv/ameesh-guest/ci/jit/r1/jit-", out)
        self.assertIn("one job, then the runner is removed by GitHub", out)
        self.assertNotIn(blob, out + res.stderr)
        self.assertNotIn(blob[:40], out + res.stderr)

    def test_deposit_refuses_a_configuration_of_another_repository(self):
        self.prepared(1)
        self.write("srv/ameesh-guest/ci/jit/r1/.keep", "")
        self.deposit_file(1, repo="https://github.com/other/repo")
        res = self.run_script("--deposit", "/root/jit-r1.b64", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("nothing deposited", res.stderr)

    def test_deposit_refuses_a_configuration_without_ephemeral(self):
        # le dépôt refuse la même chose que le portier : une configuration qui
        # laisserait un runner persistant enregistré sur l'hôte prêté
        absent = jit_config(files={".runner": json.dumps(
            {"agentName": "ameesh-nxl-ci-3-1",
             "gitHubUrl": "https://github.com/manaty/ameesh"}), ".credentials": "{}"})
        for blob in (jit_config(ephemeral=False), absent):
            self.prepared(1)
            self.write("srv/ameesh-guest/ci/jit/r1/.keep", "")
            self.deposit_file(1, blob=blob)
            res = self.run_script("--deposit", "/root/jit-r1.b64", "--host", "ci-3")
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("ephemeral true", res.stderr)
            self.assertEqual(self.pending(1), [])

    def test_deposit_refuses_a_configuration_without_runner_settings(self):
        # sans `.runner` il n'y a ni dépôt identifié ni contrat à usage unique
        # lisible : plus de repli « --runner suffit » (R1 Codex B1)
        self.prepared(1)
        self.write("srv/ameesh-guest/ci/jit/r1/.keep", "")
        self.deposit_file(1, blob=jit_config(files={".credentials": "{}"}))
        res = self.run_script("--deposit", "/root/jit-r1.b64", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no .runner", res.stderr)
        self.assertEqual(self.pending(1), [])

    def test_deposit_refuses_an_unsafe_runner_file_name(self):
        # une configuration porte des noms de fichiers écrits dans la racine
        # du runner : un chemin n'y entre jamais
        blob = jit_config(files={"../../etc/passwd": "eA=="})
        self.prepared(1)
        self.write("srv/ameesh-guest/ci/jit/r1/.keep", "")
        self.deposit_file(1, blob=blob)
        res = self.run_script("--deposit", "/root/jit-r1.b64", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("unsafe runner file name", res.stderr)
        self.assertEqual(self.pending(1), [])

    def test_deposit_refuses_a_second_deposit_of_the_same_configuration(self):
        # une configuration = un job : la déposer deux fois est une erreur, pas
        # un second job en silence
        blob = jit_config()
        self.arm(1, blob)
        self.deposit_file(1, blob=blob)
        self.assertEqual(len(self.pending(1)), 1)
        res = self.run_script("--deposit", "/root/jit-r1.b64", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("already deposited", res.stderr)
        self.assertEqual(len(self.pending(1)), 1)

    def test_deposit_binds_the_configuration_to_its_runner_slot(self):
        for n in (1, 2):
            self.prepared(n)
            self.write("srv/ameesh-guest/ci/jit/r%d/.keep" % n, "")
        # la configuration nomme le runner : c'est elle qui décide du slot
        self.deposit_file(1, name="ameesh-nxl-ci-3-2")
        res = self.run_script("--deposit", "/root/jit-r1.b64", "--runner", "1", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("names slot 2", res.stderr)
        self.assertEqual(self.pending(1), [])
        # une configuration étrangère au préfixe est refusée
        self.deposit_file(1, name="someone-elses-runner")
        res = self.run_script("--deposit", "/root/jit-r1.b64", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("names runner", res.stderr)
        # sans nom dans la configuration, --runner est exigé
        self.deposit_file(1, blob=jit_config(files={
            ".runner": json.dumps({"gitHubUrl": "https://github.com/manaty/ameesh",
                                   "ephemeral": True}),
            ".credentials": "{}"}))
        res = self.run_script("--deposit", "/root/jit-r1.b64", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--runner", res.stderr)

    def test_enable_refuses_without_a_deposited_configuration(self):
        # un démarrage = un job : sans dépôt, l'unité n'aurait rien à
        # consommer, donc --enable refuse au lieu d'armer un écouteur vide
        self.prepared(1)
        self.write("srv/ameesh-guest/ci/jit/r1/.keep", "")
        self.write("etc/ameesh/guard.env", "AMEESH_GUARD_ARGS=\n")
        res = self.run_script("--enable", "1")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no deposited JIT configuration", res.stderr)
        self.assertIn("--deposit", res.stderr)
        self.assertNotIn("systemctl start", res.stdout)

    def test_enable_arms_one_job_per_configuration(self):
        self.arm(1)
        self.write("etc/ameesh/guard.env", "AMEESH_GUARD_ARGS=\n")
        res = self.run_script("--enable", "1")
        self.assertEqual(res.returncode, 0, res.stderr)
        # `start`, pas `enable --now` : rien ne ré-arme l'unité au démarrage
        self.assertIn("systemctl start ameesh-guest-ci@1.service", res.stdout)
        self.assertNotIn("systemctl enable --now ameesh-guest-ci@1.service", res.stdout)
        self.assertIn("runs at most one job", res.stdout)

    def test_disable_refuses_a_running_job(self):
        res = self.run_script("--disable", busy=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("terminal", res.stderr)

    def test_wait_terminal_goes_with_disable_too(self):
        # the help and disable() both accept --wait-terminal; the option must
        # not be rejected before the wait is ever tried (r2 fix)
        res = self.run_script("--disable", "--wait-terminal", "30")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl disable --now ameesh-guest-ci@1.service", res.stdout)
        # a dry run never pretends the job ended: the wait still refuses
        res = self.run_script("--disable", "--wait-terminal", "30", busy=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("terminal", res.stderr)

    def test_the_count_prepared_is_adopted_by_the_later_commands(self):
        # --prepare records the runner count: --remove without --count must
        # cover every prepared runner, not fall back to the default of 2
        self.write("etc/ameesh/ci-runners.state",
                   "host ci-3\ncount 3\nversion 2.338.0\n")
        res = self.run_script("--remove")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl disable --now ameesh-guest-ci@3.service", res.stdout)
        # an explicit --count still wins over the recorded one
        res = self.run_script("--remove", "--count", "1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl disable --now ameesh-guest-ci@1.service", res.stdout)
        self.assertNotIn("ameesh-guest-ci@3.service", res.stdout)

    def test_remove_is_scoped_and_only_after_a_terminal_job(self):
        res = self.run_script("--remove", "--count", "2", busy=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("terminal", res.stderr)
        self.write("etc/ameesh/ci-runners.state",
                   "host ci-3\ncount 2\ncreated /srv/ameesh-guest/ci/jit\n")
        res = self.run_script("--remove", "--count", "2")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("systemctl disable --now ameesh-guest-ci@1.service", out)
        self.assertIn("systemctl disable --now ameesh-guest-ci@2.service", out)
        self.assertIn("rm -f ", out)
        self.assertIn("etc/systemd/system/ameesh-guest-ci@.service", out)
        # les configurations à usage unique partent avec le prêt
        self.assertIn("runuser -u ameesh-guest -- rm -rf", out)
        self.assertIn("srv/ameesh-guest/ci/jit", out)
        self.assertIn("gh api -X DELETE", out)
        for line in out.splitlines():
            if line.startswith("DRY ") or line.startswith("next,"):
                self.assertNotIn("/home/runner", line)
                self.assertNotIn("actions.runner", line)

    def test_prepare_requires_the_guard(self):
        # #1256 R1 : sans garde (ou sans couverture), rien n'arrête les
        # invités quand l'application a besoin de l'hôte : la préparation
        # refuse au lieu d'avertir
        os.remove(os.path.join(self.host, "etc/ameesh/guard.json"))
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("guard.json", res.stderr)
        self.assertIn("install-pilot.sh first", res.stderr)

    def test_enable_refuses_while_the_guard_observes(self):
        # #1256 R1 : activation fermée par défaut, enforce avant le premier
        # job ; en observation les invités restent éteints
        self.arm(1)
        self.arm(2)
        res = self.run_script("--enable")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("observation", res.stderr)
        self.assertNotIn("systemctl start", res.stdout)
        # enforce : l'activation passe
        self.write("etc/ameesh/guard.env", "AMEESH_GUARD_ARGS=\n")
        res = self.run_script("--enable")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl start ameesh-guest-ci@1.service", res.stdout)

    def test_the_archive_is_never_extracted_by_root(self):
        # #1256 R1 delta B4 : le cache et la destination d'extraction sont
        # sous $ROOT/ci, que l'invité possède. Root vérifie donc le digest sur
        # un instantané privé (0700/0600), et c'est l'invité qui extrait cet
        # instantané : jamais root depuis un chemin remplaçable.
        self.write("srv/ameesh-guest/ci/cache/actions-runner-linux-x64-2.338.0.tar.gz",
                   "not the tarball\n")
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("cached tarball reused", out)
        self.assertIn("root-private snapshot", out)
        self.assertIn("sha256sum -c -", out)          # digest sur l'instantané
        self.assertIn("runuser -u ameesh-guest -- tar -xzf - -C", out)
        self.assertNotIn("DRY tar -xzf", out)          # root n'extrait jamais
        self.assertNotIn("chown -R", out)              # plus de chown root
        self.assertNotIn("curl -fL", out)

    def test_prepare_refuses_a_symlinked_guest_path(self):
        # #1256 R1 delta B4 (binôme) : $ROOT appartient à l'invité ; un lien
        # planté sous lui ne doit pas faire sortir le prêt de son périmètre.
        # Root n'y écrit plus du tout (témoin « root never mutates »), mais
        # l'extraction et la publication invitées doivent rester dans $ROOT.
        os.symlink(os.path.join(self.host, "srv/ameesh-guest/elsewhere"),
                   os.path.join(self.host, "srv/ameesh-guest/ci"))
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("symbolic link", res.stderr)

    def test_prepare_refuses_a_symlinked_cache_entry(self):
        # même raison, sur l'entrée de cache elle-même : root la recopie dans
        # son instantané privé ; un lien lui ferait lire un autre fichier root
        self.write("srv/ameesh-guest/elsewhere.tar.gz", "not the tarball\n")
        os.makedirs(os.path.join(self.host, "srv/ameesh-guest/ci/cache"))
        os.symlink(os.path.join(self.host, "srv/ameesh-guest/elsewhere.tar.gz"),
                   os.path.join(self.host,
                                "srv/ameesh-guest/ci/cache/actions-runner-linux-x64-2.338.0.tar.gz"))
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("symbolic link", res.stderr)

    def test_prepare_refuses_a_symlinked_destination(self):
        # et sur la destination : l'extraction tourne sous l'invité, mais un
        # lien la ferait écrire hors du prêt
        os.makedirs(os.path.join(self.host, "srv/ameesh-guest/ci/elsewhere"))
        os.symlink(os.path.join(self.host, "srv/ameesh-guest/ci/elsewhere"),
                   os.path.join(self.host, "srv/ameesh-guest/ci/r1"))
        res = self.run_script("--prepare", "--host", "ci-3", "--count", "1")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("symbolic link", res.stderr)

    def test_prepare_root_never_mutates_under_the_guest_tree(self):
        # #1256 R1 delta B4 (binôme) : root n'écrit plus sous $ROOT, que
        # l'invité possède. Toute création, extraction ou publication sous
        # l'arbre invité passe par runuser ; les seules écritures root sont
        # les outils (hors arbre) et l'unité systemd.
        res = self.run_script("--prepare", "--host", "ci-3", "--count", "2")
        self.assertEqual(res.returncode, 0, res.stderr)
        guest_root = "/srv/ameesh-guest/"
        touched = [line for line in res.stdout.splitlines()
                   if line.startswith("DRY ") and guest_root in line
                   and not line.startswith("DRY state ")]     # état = /etc/ameesh
        self.assertTrue(touched, "aucune mutation sous l'arbre invité ?")
        for line in touched:
            self.assertTrue(line.startswith("DRY runuser -u ameesh-guest -- "),
                            "root muterait sous l'arbre invité : %s" % line)
        wrote = [line for line in res.stdout.splitlines()
                 if line.startswith("DRY write ")]
        self.assertTrue(wrote)
        for line in wrote:
            self.assertNotIn(guest_root, line)                # jamais sous $ROOT
            self.assertTrue(TOOLS + "/" in line or "/etc/systemd/system/" in line,
                            "écriture root inattendue : %s" % line)

    def test_prepare_refuses_a_symlinked_tools_directory(self):
        # #1256 R1 delta B4 (binôme) : les outils privilégiés vivent hors de
        # l'arbre invité. Si le chemin des outils est un lien — le parent a
        # été échangé après le contrôle — root n'écrit pas : refus avant tout.
        shutil.rmtree(os.path.join(self.host, TOOLS_REL))
        os.makedirs(os.path.join(self.host, "elsewhere/ameesh-guest-ci"))
        os.symlink(os.path.join(self.host, "elsewhere/ameesh-guest-ci"),
                   os.path.join(self.host, TOOLS_REL))
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("unsafe root tools directory", res.stderr)
        self.assertNotIn("DRY write %s/" % TOOLS, res.stdout)

    def test_prepare_refuses_a_swapped_tools_ancestor(self):
        # même attaque, un cran plus haut : un ancêtre de la chaîne remplacé
        # par un lien. Le répertoire des outils existe encore *à travers* le
        # lien : seul le contrôle de chaîne (lstat, composant par composant)
        # l'attrape.
        shutil.rmtree(os.path.join(self.host, "usr/local/lib"))
        os.makedirs(os.path.join(self.host, "usr/local/lib-real/ameesh-guest-ci"))
        self.write("usr/local/lib-real/ameesh-guest-ci/docker",
                   "#!/bin/sh\n# lot #1256\nexit 0\n", mode=0o755)
        os.symlink(os.path.join(self.host, "usr/local/lib-real"),
                   os.path.join(self.host, "usr/local/lib"))
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("unsafe root tools directory", res.stderr)

    def test_prepare_refuses_a_symlinked_tool_file(self):
        # un outil remplacé par un lien vers un fichier qui porte même le
        # marqueur du lot : le contrôle de type (fichier régulier, pas un
        # lien) passe avant la reprise d'un shim existant
        self.write("elsewhere/docker", "#!/bin/sh\n# lot #1256\nexit 0\n", mode=0o755)
        os.remove(os.path.join(self.host, TOOLS_REL, "docker"))
        os.symlink(os.path.join(self.host, "elsewhere/docker"),
                   os.path.join(self.host, TOOLS_REL, "docker"))
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("unsafe root tools file", res.stderr)

    def test_the_tools_guard_refuses_a_chain_the_guest_could_change(self):
        # Le seam de test (CI_RUNNERS_TEST_HOST) saute le contrôle root:root /
        # non inscriptible, qui ne peut pas s'exercer sur un hôte de test non
        # root : on exerce donc le vérificateur embarqué tel quel, sans seam.
        # Une chaîne possédée par un autre utilisateur est refusée ; avec le
        # seam, la même chaîne passe — le contrôle n'est pas décoratif.
        with open(SCRIPT, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("<<'PYSAFE'", src)
        guard = src.split("<<'PYSAFE'\n", 1)[1].split("\nPYSAFE\n", 1)[0]
        path = os.path.join(self.host, TOOLS_REL)
        for seam, ok in (("", False), (self.host, True)):
            res = subprocess.run([sys.executable, "-I", "-", path, seam],
                                 input=guard, capture_output=True, text=True)
            if ok:
                self.assertEqual(res.returncode, 0, res.stderr)
            else:
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("unsafe root tools", res.stderr)

    def test_remove_refuses_a_tools_parent_swapped_after_prepare(self):
        # le retrait est protégé de la même façon (R1 delta B4, binôme) : un
        # parent échangé après --prepare fait refuser --remove avant tout rm
        # root, au lieu de suivre le lien
        shutil.rmtree(os.path.join(self.host, TOOLS_REL))
        os.makedirs(os.path.join(self.host, "elsewhere/ameesh-guest-ci"))
        os.symlink(os.path.join(self.host, "elsewhere/ameesh-guest-ci"),
                   os.path.join(self.host, TOOLS_REL))
        self.write("etc/ameesh/ci-runners.state",
                   "host ci-3\ncount 1\ncreated %s/docker\n" % TOOLS)
        res = self.run_script("--remove", "--count", "1")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("unsafe root tools path", res.stderr)
        self.assertNotIn("DRY rm -f", res.stdout)

    def test_remove_returns_the_tools_only_when_it_created_them(self):
        # #1256 R1 delta B4 (binôme) : le retrait ne suit que l'état écrit par
        # --prepare ; les outils sont retirés hors de l'arbre invité, et leur
        # répertoire seulement s'il est vide.
        self.write("etc/ameesh/ci-runners.state",
                   "host ci-3\ncount 1\n"
                   "created %s\ncreated %s/docker\ncreated %s/ci-runner-gate\n"
                   "created /srv/ameesh-guest/ci/r1\n"
                   % (TOOLS, TOOLS, TOOLS))
        res = self.run_script("--remove", "--count", "1")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout
        self.assertIn("runuser -u ameesh-guest -- rm -rf ", out)
        self.assertIn("srv/ameesh-guest/ci/r1", out)
        self.assertIn("rm -f ", out)
        self.assertIn(TOOLS + "/docker", out)
        self.assertIn(TOOLS + "/ci-runner-gate", out)
        self.assertIn("rmdir --ignore-fail-on-non-empty", out)
        self.assertIn(TOOLS, out)
        # root ne suit jamais un chemin sous l'arbre invité pour un rm
        for line in out.splitlines():
            if line.startswith("DRY rm ") and "/srv/ameesh-guest/" in line:
                self.fail("rm root sous l'arbre invité : %s" % line)

    def test_the_recorded_disk_floor_is_adopted_by_the_later_commands(self):
        # #1256 R1 delta B5 : le plancher qualifié au --prepare est enregistré ;
        # le dépôt ne retombe pas sur le défaut 15360 qui refuserait la ci-3
        # mesurée (8.95 GiB libres).
        self.prepared(1)
        self.write("srv/ameesh-guest/ci/jit/r1/.keep", "")
        self.deposit_file(1)
        self.write("etc/ameesh/ci-runners.state", "host ci-3\ncount 2\nfloor 1\n")
        res = self.run_script("--deposit", "/root/jit-r1.b64", "--host", "ci-3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("deposited: r1", res.stdout)
        # une valeur enregistrée trop haute est reprise telle quelle : refus,
        # jamais un repli silencieux sur le défaut
        self.write("etc/ameesh/ci-runners.state", "host ci-3\ncount 2\nfloor 999999\n")
        res = self.run_script("--deposit", "/root/jit-r1.b64", "--host", "ci-3")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("999999 MiB floor", res.stderr)
        # l'option explicite l'emporte sur la valeur enregistrée
        res = self.run_script("--deposit", "/root/jit-r1.b64", "--disk-floor-mib", "1", "--host", "ci-3")
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_prepare_installs_the_gate_wrapper(self):
        # #1256 R1 : l'unité ne lance pas run.sh directement ; --prepare pose
        # le garde-barrière qui obéit à la porte
        res = self.run_script("--prepare", "--host", "ci-3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DRY write %s/ci-runner-gate (0755 root:root)" % TOOLS,
                      res.stdout)
        self.assertIn("capacity_guard", res.stdout)   # le contrat, pas un doublon
        self.assertIn("gate available: starting", res.stdout)

    def test_remove_returns_the_gate_wrapper(self):
        self.write("etc/ameesh/ci-runners.state",
                   "host ci-3\ncount 1\n"
                   "created %s/ci-runner-gate\n" % TOOLS)
        res = self.run_script("--remove", "--count", "1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("rm -f", res.stdout)
        self.assertIn(TOOLS + "/ci-runner-gate", res.stdout)

    def test_check_accepts_a_prepared_slot_without_credentials(self):
        # option A : aucun `.runner`/`.credentials` en dehors d'un job ; un
        # emplacement préparé, sans dépôt, est sain (il attend un dépôt)
        self.prepared(1)
        res = self.run_script("--check", "--count", "1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("state: stopped (no document yet", res.stdout)
        self.assertIn("wrapper: %s/ci-runner-gate is missing" % TOOLS, res.stdout)
        self.assertIn("deposited configurations: 0 (one job each)", res.stdout)
        self.assertIn("check: every line ok", res.stdout)

    def test_check_refuses_a_dead_credential_left_on_disk(self):
        # un `.runner`/`.credentials` laissé par une exécution tuée est le
        # credential durable que l'option A supprime : KO, chemin nommé
        self.prepared(1)
        self.write("srv/ameesh-guest/ci/r1/.runner", "{}\n")
        self.write("srv/ameesh-guest/ci/r1/.credentials", "{}\n", mode=0o600)
        res = self.run_script("--check", "--count", "1")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("r1: .runner is left on disk while no unit runs", res.stderr)
        self.assertIn("r1: .credentials is left on disk while no unit runs", res.stderr)

    def test_check_proves_the_application_is_out_of_the_guest_s_read(self):
        # R2 #1256 B2 : un job de l'application est root-equivalent sur cette
        # VM (Docker) ; `--check` sonde donc en tant qu'invité la racine des
        # runners, leurs secrets et leurs arbres de travail, et exige l'échec
        # de `test -r` sur chacun.
        self.prepared(1)
        self.write("home/runner/r1/.runner", "{}\n", mode=0o600)
        self.write("home/runner/r1/.credentials", "secret\n", mode=0o600)
        self.write("home/runner/r1/_work/nexlink/README", "x\n", mode=0o644)
        res = self.run_script("--check", "--count", "1")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        for path in ("/home/runner", "/home/runner/r1/.runner",
                     "/home/runner/r1/.credentials", "/home/runner/r1/_work"):
            self.assertIn("ok ameesh-guest cannot read %s" % path, res.stdout)

    def test_check_refuses_an_application_secret_the_guest_can_read(self):
        # le KO nomme le chemin fautif, pas seulement un compte
        self.prepared(1)
        self.write("home/runner/r1/.credentials", "secret\n", mode=0o600)
        res = self.run_script(
            "--check", "--count", "1",
            CI_RUNNERS_TEST_GUEST_READS="/home/runner/r1/.credentials")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("KO ameesh-guest can read /home/runner/r1/.credentials",
                      res.stderr)

    def gate_document(self, state="stopped"):
        return self.write("run/ameesh-gate/state/state.json", json.dumps(
            {"schema": "ameesh-host-state/1", "state": state, "seq": 3,
             "expires_ts": time.time() + 90, "guard": {"mode": "enforce"}}))

    def test_check_accepts_the_wrapper_waiting_for_a_closed_gate(self):
        # C2 : le wrapper attend la réouverture sans consommer de
        # configuration ; une unité active sans processus runner n'est pas une
        # désobéissance.
        self.write(TOOLS_REL + "/ci-runner-gate", "#!/bin/sh\nexit 0\n", mode=0o755)
        self.gate_document()
        for n in (1, 2):
            self.prepared(n)
        res = self.run_script("--check", CI_RUNNERS_TEST_ACTIVE="1,2")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("state: stopped", res.stdout)
        self.assertIn("the wrapper waits for the gate", res.stdout)

    def test_check_refuses_a_runner_alive_after_the_stop_delay(self):
        # C2 : au-delà du délai d'arrêt du wrapper, un processus runner vivant
        # sous une unité active n'est plus un drainage, c'est une désobéissance.
        self.write(TOOLS_REL + "/ci-runner-gate", "#!/bin/sh\nexit 0\n", mode=0o755)
        self.gate_document()
        self.prepared(1)
        res = self.run_script("--check", "--count", "1",
                              CI_RUNNERS_TEST_ACTIVE="1",
                              CI_RUNNERS_TEST_CHILD="1",
                              CI_RUNNERS_TEST_STOP_DELAY="0")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("waiting up to 0s", res.stdout)
        self.assertIn("still runs after the wrapper's stop delay", res.stderr)
        self.assertNotIn("unbound variable", res.stderr)

    def test_check_stop_delay_defaults_without_the_seam(self):
        # C3 : sans seam, le délai reste un nombre (aucun `unbound variable`)
        # et le refus garde sa raison ; le défaut des dry runs est zéro.
        self.write(TOOLS_REL + "/ci-runner-gate", "#!/bin/sh\nexit 0\n", mode=0o755)
        self.gate_document()
        self.prepared(1)
        res = self.run_script("--check", "--count", "1",
                              CI_RUNNERS_TEST_ACTIVE="1",
                              CI_RUNNERS_TEST_CHILD="1")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("waiting up to 0s", res.stdout)
        self.assertIn("still runs after the wrapper's stop delay", res.stderr)
        self.assertNotIn("unbound variable", res.stderr)

    def test_check_accepts_a_runner_stopped_within_the_delay(self):
        # C2 : un child vivant au premier relevé mais arrêté dans le délai du
        # wrapper est un drainage obéi ; le chemin positif est couvert. Le
        # marqueur est posé après l'entrée attestée dans l'attente (stdout),
        # pas après un `sleep` : la vitesse de la machine ne décide pas.
        self.write(TOOLS_REL + "/ci-runner-gate", "#!/bin/sh\nexit 0\n", mode=0o755)
        self.gate_document()
        self.prepared(1)
        proc = self.start_script("--check", "--count", "1",
                                 CI_RUNNERS_TEST_ACTIVE="1",
                                 CI_RUNNERS_TEST_CHILD="1",
                                 CI_RUNNERS_TEST_STOP_DELAY="30")
        seen = self.sync_on_stdout(proc, b"waiting up to 30s")
        self.write("run/ci-runners-test-child-gone-1", "")
        out, err = self.finish_script(proc, seen)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertIn("the runner process is gone", out)
        self.assertNotIn("KO", err)

    def test_check_reads_the_gate_again_and_accepts_a_reopening(self):
        # C2 résiduel : la porte est relue pendant l'attente ; une réouverture
        # available/enforce rend le child légitime au lieu d'un KO sur la
        # porte initialement fermée (le child simulé reste vivant). La
        # réouverture est écrite après l'entrée attestée dans l'attente : une
        # porte déjà ouverte au relevé initial ne prendrait pas ce chemin.
        self.write(TOOLS_REL + "/ci-runner-gate", "#!/bin/sh\nexit 0\n", mode=0o755)
        self.gate_document()
        self.prepared(1)
        proc = self.start_script("--check", "--count", "1",
                                 CI_RUNNERS_TEST_ACTIVE="1",
                                 CI_RUNNERS_TEST_CHILD="1",
                                 CI_RUNNERS_TEST_STOP_DELAY="10")
        seen = self.sync_on_stdout(proc, b"waiting up to 10s")
        self.gate_document(state="available")
        out, err = self.finish_script(proc, seen)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertIn("the gate reopened while waiting", out)
        self.assertNotIn("still runs", err)

    def test_the_documented_offer_counts_jobs(self):
        # le brouillon de fiche d'hôte du plan est lu par le contrat : le
        # signal de jobs y est `busy_tasks`, pas le nombre d'écouteurs
        with open(PLAN, encoding="utf-8") as fh:
            plan = fh.read()
        # the plan carries more than one yaml block (the routing expression,
        # the Host fiche): the offer is the one with `policy:`
        blocks = [block.split("```", 1)[0] for block in plan.split("```yaml")[1:]]
        block = next(block for block in blocks if "policy:" in block)
        body = "\n".join(line for line in textwrap.dedent(block).splitlines()
                         if line.strip() != "---")
        lend = canon.load_yaml(body)["policy"]["lend"]
        offer, problems, unknown = capacity_guard.parse_lend(lend)
        self.assertEqual(problems, [])
        # `restart_after_seconds` est la clé propre au garde (L159e4), lue par
        # `guard.parse_offer` : le lecteur du contrat la signale, pas plus
        self.assertEqual(unknown, ["restart_after_seconds"])
        reclaim = offer["reclaim"]
        self.assertEqual(reclaim["busy_tasks"], ["Runner.Worker"])
        self.assertEqual(reclaim["busy_units"], ["actions.runner.*"])
        # l'application garde au moins un runner : l'invité s'arrête avant
        self.assertLess(reclaim["busy_max"], 4)
        # #1256 : chiffres qualifiés sur la ci-3 mesurée (8.95 GiB libres),
        # pas l'offre initiale (30 GiB / 10 GiB) que l'hôte ne peut pas tenir
        self.assertEqual(offer["disk_max"], 4 * 2 ** 30)
        self.assertEqual(reclaim["disk_free_min"], 5 * 2 ** 30)
        # le plancher de préparation du plan suit la réserve du garde
        self.assertIn("--prepare --count 1 --disk-floor-mib 6144", plan)
        # #1256 R1 delta B5 : le dépôt du plan répète le plancher qualifié, il
        # ne retombe pas sur le défaut 15 GiB
        self.assertIn("--disk-floor-mib 6144 --deposit", plan)
        self.assertNotIn("disk_max: 30GiB", plan)


class WorkflowRoutingTest(unittest.TestCase):
    """Option A (#1256): the borrowed label is never the fleet's, and only a
    job outside the required path (`tests`, the only required status check)
    may target it. These witnesses read the workflows and the plan, so a
    later `runs-on` edit cannot quietly move a required job onto a lending
    host."""

    REQUIRED = "tests"

    def workflows(self):
        root = os.path.join(REPO, ".github", "workflows")
        out = {}
        for name in sorted(os.listdir(root)):
            if name.endswith((".yml", ".yaml")):
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    out[name] = canon.load_yaml(fh.read()) or {}
        return out

    def needs_closure(self, jobs, job):
        seen = set()
        stack = [job]
        while stack:
            current = stack.pop()
            needs = jobs.get(current, {}).get("needs") or []
            if isinstance(needs, str):
                needs = [needs]
            for parent in needs:
                if parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        return seen

    def routed(self):
        """(workflow, job, spec) of every job whose runs-on reads the
        borrowed-label variable."""
        for name, doc in self.workflows().items():
            for job, spec in (doc.get("jobs") or {}).items():
                if "AMEESH_LENT_RUNS_ON" in str(spec.get("runs-on", "")):
                    yield name, job, spec

    def test_the_borrowed_label_is_never_a_literal_of_a_job(self):
        # on ne route que par la variable d'opérateur : aucun `runs-on` ne
        # nomme le label en dur, donc aucune faute de frappe ne peut envoyer
        # un job sur un hôte prêté, et le retrait se fait en une variable
        for name, doc in self.workflows().items():
            for job, spec in (doc.get("jobs") or {}).items():
                self.assertNotIn(LENT, str(spec.get("runs-on", "")),
                                 "%s/%s names the borrowed label directly" % (name, job))

    def test_only_non_gating_jobs_outside_the_required_path_are_routed(self):
        routed = list(self.routed())
        self.assertEqual(sorted("%s/%s" % (name, job) for name, job, _ in routed),
                         ["tests.yml/bilan", "ui.yml/ui"])
        tests = self.workflows()["tests.yml"]["jobs"]
        closure = self.needs_closure(tests, self.REQUIRED) | {self.REQUIRED}
        for name, job, spec in routed:
            self.assertNotIn(job, closure,
                             "%s/%s is needed by the required check" % (name, job))
            doc = self.workflows()[name]
            perms = doc.get("permissions", {})
            if isinstance(perms, str):
                self.assertIn(perms, ("read-all", "none"))
            else:
                for scope, value in perms.items():
                    self.assertNotEqual(value, "write", "%s grants write" % scope)
            job_perms = spec.get("permissions", {})
            if isinstance(job_perms, str):
                self.assertIn(job_perms, ("read-all", "none"))
            else:
                for scope, value in job_perms.items():
                    self.assertNotEqual(value, "write", "%s/%s grants write" % (name, scope))
            # nothing else depends on it in its own workflow
            for other, other_spec in (doc.get("jobs") or {}).items():
                self.assertNotIn(job, other_spec.get("needs", []) or [],
                                 "%s/%s feeds %s" % (name, job, other))

    def test_a_routed_job_can_never_write_the_actions_cache(self):
        """Le canal du cache (R1 Codex B3) : un job prêté reçoit le jeton
        d'exécution du run, que `permissions:` ne borne pas ; sans déclaration,
        un déclencheur de confiance lui donne l'écriture du cache. Or `plan`
        restaure `carte-*` et les parts psycopg `.venv-ci`, donc une entrée
        forgée depuis l'hôte prêté atteindrait les checks requis. La parade est
        `cache-mode`, appliqué par le service de cache : chaque job routé le
        déclare, et jamais en écriture."""
        for name, job, spec in self.routed():
            mode = spec.get("cache-mode")
            self.assertIn(mode, ("read", "none"),
                          "%s/%s does not close cache writes (cache-mode %r)"
                          % (name, job, mode))
        # le témoin lit aussi les fichiers : `cache-mode` est une clé de job,
        # pas une variable d'environnement qu'un job pourrait poser lui-même
        with open(os.path.join(REPO, ".github", "workflows", "tests.yml"),
                  encoding="utf-8") as fh:
            tests_src = fh.read()
        self.assertIn("cache-mode: none", tests_src)
        with open(os.path.join(REPO, ".github", "workflows", "ui.yml"),
                  encoding="utf-8") as fh:
            ui_src = fh.read()
        self.assertIn("cache-mode: read", ui_src)
        for src in (tests_src, ui_src):
            self.assertNotIn("cache-mode: write", src)
            self.assertNotIn("cache-mode: write-only", src)

    def test_the_required_path_verifies_the_plan_artifact(self):
        """Le jeton d'exécution sert aussi l'API d'artefacts du run : un job
        routé (`bilan`) tourne après `plan` et peut remplacer l'artefact
        `plan` avant un « re-run failed jobs » des parts (R1 dev4 B4, #1256).
        Le job `plan` publie donc l'empreinte du fichier qu'il dépose en
        sortie de job — que nul autre job ne peut réécrire — et chaque
        consommateur requis la vérifie avant d'exécuter."""
        jobs = self.workflows()["tests.yml"]["jobs"]
        plan = jobs["plan"]
        self.assertIn("plan_sha256", plan.get("outputs") or {})
        self.assertIn("sha256sum", structure_text(plan))
        closure = self.needs_closure(jobs, self.REQUIRED) | {self.REQUIRED}
        consumers = []
        for job in sorted(closure):
            steps = jobs[job].get("steps") or []
            downloads = [s for s in steps
                         if str(s.get("uses", "")).startswith("actions/download-artifact")
                         and (s.get("with") or {}).get("name") == "plan"]
            if not downloads:
                continue
            consumers.append(job)
            body = " ".join(structure_text(steps).split())
            self.assertIn("plan.outputs.plan_sha256", body,
                          "%s downloads the plan artifact without checking its digest" % job)
            self.assertIn("sha256sum -c", body)
        self.assertEqual(set(consumers), {"psycopg", "psql", "fs-class"})

    def test_the_required_check_stays_on_the_fleet(self):
        jobs = self.workflows()["tests.yml"]["jobs"]
        self.assertEqual(jobs[self.REQUIRED].get("name"), self.REQUIRED)
        closure = self.needs_closure(jobs, self.REQUIRED) | {self.REQUIRED}
        self.assertEqual(closure, {"plan", "version", "psycopg", "psql",
                                   "platform-macos", "doc-readers", "fs-class", "tests"})
        for job in sorted(closure):
            self.assertNotIn("AMEESH_LENT_RUNS_ON", str(jobs[job].get("runs-on", "")),
                             "%s is part of the required path" % job)

    def test_the_plan_documents_the_routing_rule(self):
        with open(PLAN, encoding="utf-8") as fh:
            plan = fh.read()
        self.assertIn("AMEESH_LENT_RUNS_ON", plan)
        self.assertIn(LENT, plan)
        self.assertIn("single-use", plan)
        # la règle, et ce qui ne peut pas être routé
        self.assertIn("the fleet's `ameesh-ci`", plan)
        for forbidden in ("plan", "carte"):
            self.assertIn("`%s`" % forbidden, plan)


if __name__ == "__main__":
    unittest.main()
