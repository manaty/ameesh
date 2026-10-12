# SPDX-License-Identifier: AGPL-3.0-only
"""#502 — `poste.sh redemarrer` and `vm.sh redemarrer` check that a restarted runner
takes its lease again.

On 2026-10-12, deploy-464 restarted ameesh-dev3; its unit stayed active but the
runner never took its lease again, and the deploy still ended "OK". Fake
`systemctl`, `sleep` and `ameesh` on PATH; no real unit, no database.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "deploy", "mise-a-jour", "poste.sh")

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import os, sys, time
args = [a for a in sys.argv[1:] if a not in ("--user", "--quiet", "--plain", "--no-legend")]
state = os.environ["FAKE_STATE"]
if args[0] == "list-units":
    print("ameesh-runner-agent@dev9.service loaded active running runner")
elif args[0] == "show":
    print("{ path=/x ; argv[]=/x run --agents dev9 ; }")
elif args[0] == "restart":
    with open(os.path.join(state, "restarted"), "w") as fh:
        fh.write(str(time.time()))
elif args[0] == "is-active":
    print("active")
'''

# Before the restart the old runner holds epoch 7. Afterwards: "lease" =
# released (8) then claimed (9); "no-lease" = released, never claimed (dev3 on
# 2026-10-12); "stale-lease" = never released nor claimed again.
FAKE_AMEESH = r'''#!/bin/sh
[ "$1 $2" = "show dev9" ] || exit 0
owner='"runner-dev9@host"' epoch=7
if [ -e "$FAKE_STATE/restarted" ]; then
  case $(cat "$FAKE_STATE/scenario") in
    lease) epoch=9 ;;
    no-lease) owner=null epoch=8 ;;
  esac
fi
printf '{"name": "dev9", "status": "idle", "lease_owner": %s, "lease_epoch": %s}\n' "$owner" "$epoch"
'''


class RestartLeaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-502-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        home = os.path.join(self.tmp, "home")
        bin_dir = os.path.join(self.tmp, "bin")
        venv_bin = os.path.join(home, ".local", "share", "ameesh", "venv", "bin")
        conf = os.path.join(home, ".config", "ameesh")
        self.state = os.path.join(self.tmp, "state")
        for path in (bin_dir, venv_bin, conf, self.state):
            os.makedirs(path)
        with open(os.path.join(conf, "bascule.env"), "w", encoding="utf-8") as fh:
            fh.write("VM=vm.invalid\n")
        for path, text in ((os.path.join(bin_dir, "systemctl"), FAKE_SYSTEMCTL),
                           (os.path.join(bin_dir, "sleep"), "#!/bin/sh\nexit 0\n"),
                           (os.path.join(venv_bin, "ameesh"), FAKE_AMEESH)):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(path, 0o755)
        self.env = dict(os.environ, HOME=home, FAKE_STATE=self.state,
                        PATH=bin_dir + os.pathsep + os.environ["PATH"])
        self.env.pop("UNITES_RESTE", None)

    def restart(self, scenario):
        with open(os.path.join(self.state, "scenario"), "w", encoding="utf-8") as fh:
            fh.write(scenario)
        return subprocess.run(["bash", SCRIPT, "redemarrer"], env=self.env,
                              capture_output=True, text=True, timeout=300)

    def test_runner_that_takes_its_lease_again_passes(self):
        proc = self.restart("lease")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dev9 : redémarré", proc.stdout)

    def test_active_unit_without_its_lease_fails_the_restart(self):
        for scenario in ("no-lease", "stale-lease"):
            with self.subTest(scenario=scenario):
                os.makedirs(self.state, exist_ok=True)
                proc = self.restart(scenario)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("dev9", proc.stderr)
                self.assertIn("retour", proc.stderr)
                self.assertNotIn("dev9 : redémarré", proc.stdout)
                os.remove(os.path.join(self.state, "restarted"))


# vm.sh restarts the server's system units over ssh: the fake ssh answers the
# migration check (one migration, applied) and the restart itself.
FAKE_SSH = r'''#!/bin/sh
for last; do :; done
case $last in
  *"rev-parse HEAD"*) echo abc123 ;;
  *ls-tree*) echo src/ameesh/migrations/0001_init.sql ;;
  *psql*) echo 1 ;;
  *"systemctl restart"*) : > "$FAKE_STATE/restarted" ;;
esac
'''


class VmRestartLeaseTest(RestartLeaseTest):
    def setUp(self):
        super().setUp()
        bin_dir = self.env["PATH"].split(os.pathsep)[0]
        with open(os.path.join(bin_dir, "ssh"), "w", encoding="utf-8") as fh:
            fh.write(FAKE_SSH)
        os.chmod(os.path.join(bin_dir, "ssh"), 0o755)
        conf = os.path.join(self.env["HOME"], ".config", "ameesh", "bascule.env")
        with open(conf, "a", encoding="utf-8") as fh:
            fh.write("PERSONAS_VM=dev9\n")

    def restart(self, scenario):
        with open(os.path.join(self.state, "scenario"), "w", encoding="utf-8") as fh:
            fh.write(scenario)
        return subprocess.run(["bash", os.path.join(os.path.dirname(SCRIPT), "vm.sh"),
                               "redemarrer"], env=self.env, capture_output=True, text=True,
                              timeout=300)


if __name__ == "__main__":
    unittest.main()
