# SPDX-License-Identifier: AGPL-3.0-only
"""#2950 : dispatcher réel, cgroups simulés, processus Python vivants isolés."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests import support  # noqa: F401 — environnement isolé du poste

from ameesh import platform


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/mise-a-jour/poste.sh"
SYSTEMCTL = '''#!/bin/sh
printf '%s\n' "$*" >> "$TEST_LOG"
case "$*" in
  *is-active*) exit "${NOTIFY_ACTIVE_STATUS:-0}" ;;
  *MainPID*)
    if [ -f "$TEST_PID_READ" ]; then cat "$TEST_PID_AFTER";
    else touch "$TEST_PID_READ"; cat "$TEST_PID"; fi ;;
esac
'''


class PosteProcessusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="poste-2950-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.share = self.home / ".local/share/ameesh"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.venv = self.share / "venv"
        (self.venv / "bin").mkdir(parents=True)
        self.current = self.share / "venv-new"
        self.old = self.share / "venv-old"
        for venv in (self.current, self.old):
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").symlink_to(sys.executable)
        (self.share / "venv-courant").symlink_to(self.current.name)
        self.write_script(self.venv / "bin/ameesh", "#!/bin/sh\nexit 0\n")
        self.write_script(self.bin / "systemctl", SYSTEMCTL)
        self.write_script(self.bin / "sleep", "#!/bin/sh\nexit 0\n")
        # Ne jamais arrêter une vraie unité depuis un test : systemctl est
        # toujours simulé, le cgroup décrit uniquement le contexte du scénario.
        self.write_script(self.bin / "cat", '''#!/bin/sh
case "$1" in
  /proc/*/cgroup) exec /usr/bin/cat "$TEST_CGROUP" ;;
  *) exec /usr/bin/cat "$@" ;;
esac
''')
        self.cgroup = self.root / "cgroup"
        self.cgroup.write_text("0::/user.slice/session.scope\n")
        self.pid = self.root / "pid"
        self.pid.write_text("0\n")
        (self.root / "bascule.env").write_text("VM=simulation\n")
        self.log = self.root / "systemctl.log"
        self.env = dict(os.environ, HOME=str(self.home),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        BASCULE_ENV=str(self.root / "bascule.env"),
                        UNITES_RESTE="", TEST_CGROUP=str(self.cgroup),
                        TEST_LOG=str(self.log), TEST_PID=str(self.pid),
                        TEST_PID_AFTER=str(self.pid),
                        TEST_PID_READ=str(self.root / "pid-read"))

    @staticmethod
    def write_script(path, source):
        path.write_text(source)
        path.chmod(0o755)

    def run_poste(self, action):
        return subprocess.run(["bash", str(SCRIPT), action], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def process(self, venv):
        child = subprocess.Popen([str(venv / "bin/python"), "-c",
                                  "import sys; print('ready', flush=True); sys.stdin.read()"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.close_process, child)
        self.assertEqual(child.stdout.readline(), "ready\n")
        self.pid.write_text(str(child.pid) + "\n")
        return child

    @staticmethod
    def close_process(child):
        if child.poll() is None:
            child.terminate()
        child.communicate(timeout=5)

    def test_agent_cgroup_refuses_before_any_systemctl_action(self):
        for cgroup in (
            "0::/user.slice/app.slice/ameesh-release.service\n",
            "0::/user.slice/ameesh-runner@x.service\n",
            "0::/user.slice/ameesh-runner-agent@dev9.service\n",
            "0::/user.slice/ameesh-runner-agent@dev9.service/turn.scope\n",
            "0::/user.slice/ameesh-dev9.service\n",
            "0::/user.slice/ameesh-turn.scope\n",
            "5:memory:/user.slice/ameesh-dev9.service\n0::/session.scope\n",
        ):
            for action in ("redemarrer", "retour"):
                with self.subTest(cgroup=cgroup, action=action):
                    self.cgroup.write_text(cgroup)
                    done = self.run_poste(action)
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn("redémarrage refusé depuis une unité ameesh-*", done.stderr)
                    self.assertIn("systemd-run", done.stderr)
                    self.assertFalse(self.log.exists())

    def test_unknown_cgroup_refuses_before_any_stop(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.cgroup.write_text("")
                if missing:
                    self.cgroup.unlink()
                done = self.run_poste("redemarrer")
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("redémarrage refusé", done.stderr)
                self.assertFalse(self.log.exists())

    def test_independent_shell_and_emergency_transient_can_restart(self):
        for cgroup in ("0::/user.slice/session.scope\n",
                       "0::/user.slice/app.slice/poste-maj-urgence-123.service\n"):
            with self.subTest(cgroup=cgroup):
                self.cgroup.write_text(cgroup)
                done = self.run_poste("redemarrer")
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                self.assertIn("--user restart ameesh-notify.service", self.log.read_text())

    def test_notify_current_process_passes(self):
        self.process(self.current)
        done = self.run_poste("controler")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("MainPID vivant sur le venv courant", done.stdout)
        self.assertIn("contrôles verts", done.stdout)

    @unittest.skipUnless(platform.is_linux(), "témoin /proc/<pid>/exe : Linux seulement")
    def test_notify_old_process_fails_despite_same_system_executable(self):
        child = self.process(self.old)
        self.assertTrue((Path("/proc") / str(child.pid) / "exe").samefile(
            self.current / "bin/python"))
        done = self.run_poste("controler")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("venv courant non prouvé", done.stderr)
        self.assertNotIn("contrôles verts", done.stdout)

    def test_notify_inactive_or_missing_pid_fails(self):
        for pid, active in (("0", "0"), ("", "0"), ("invalid", "0"),
                            ("999999999", "0"), ("0", "3")):
            with self.subTest(pid=pid, active=active):
                self.pid.write_text(pid + "\n")
                self.env["NOTIFY_ACTIVE_STATUS"] = active
                done = self.run_poste("controler")
                self.assertNotEqual(done.returncode, 0)
                self.assertNotIn("contrôles verts", done.stdout)

    def test_notify_mainpid_change_fails(self):
        self.process(self.current)
        other = self.root / "other-pid"
        other.write_text("0\n")
        self.env["TEST_PID_AFTER"] = str(other)
        done = self.run_poste("controler")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("MainPID changé", done.stderr)
        self.assertNotIn("contrôles verts", done.stdout)

    def test_notify_before_first_versioned_venv(self):
        (self.share / "venv-courant").unlink()
        (self.venv / "bin/python").symlink_to(sys.executable)
        self.process(self.venv)
        done = self.run_poste("controler")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_notify_broken_current_link_fails(self):
        self.process(self.current)
        (self.share / "venv-courant").unlink()
        (self.share / "venv-courant").symlink_to("absent")
        done = self.run_poste("controler")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("contrôles verts", done.stdout)
