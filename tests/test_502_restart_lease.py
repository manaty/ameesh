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

PASSWORD = "s3cret-pw"

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import os, sys, time
args = [a for a in sys.argv[1:] if a not in ("--user", "--quiet", "--plain", "--no-legend")]
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "argv.log"), "a") as fh:
    fh.write(" ".join(sys.argv) + "\n")
if args[0] == "list-units":
    print("ameesh-runner-agent@dev9.service loaded active running runner")
elif args[0] == "show":
    path = os.path.join(state, "execstart")
    argv = (open(path).read().strip() if os.path.exists(path)
            else "/x run --agents dev9 --runner-id runner-dev9@host")
    if argv != "-":   # "-": ExecStart unreadable
        print("{ path=/x ; argv[]=%s ; }" % argv)
elif args[0] == "restart":
    with open(os.path.join(state, "restarted"), "w") as fh:
        fh.write(str(time.time()))
elif args[0] == "is-active":
    print("active")
'''

# Before the restart the unit's runner (runner-dev9@host, its --runner-id)
# holds epoch 7; the database clock reads 1000 (fake ssh). Afterwards: "lease" =
# released (8) then claimed again (9); "no-lease" = released, never claimed
# (dev3 on 2026-10-12); "stale-lease" = never released nor claimed again;
# "other-owner" = epoch 9 taken by someone else; "expired" = claimed again but
# already expired at the database clock; "not-held" = no lease before nor after
# (a persona at rest, not eligible): not checked.
FAKE_AMEESH = r'''#!/bin/sh
echo "$0 $*" >> "$FAKE_STATE/argv.log"
[ "$1 $2" = "show dev9" ] || exit 0
scenario=$(cat "$FAKE_STATE/scenario")
owner='"runner-dev9@host"' epoch=7 expires=1300
[ "$scenario" = not-held ] && owner=null
if [ -e "$FAKE_STATE/restarted" ]; then
  case $scenario in
    lease) epoch=9 ;;
    no-lease) owner=null epoch=8 ;;
    other-owner) owner='"attach-dev9@elsewhere"' epoch=9 ;;
    expired) epoch=9 expires=900 ;;
  esac
fi
printf '{"name": "dev9", "status": "idle", "lease_owner": %s, "lease_epoch": %s, "lease_expires_ts": %s}\n' \
  "$owner" "$epoch" "$expires"
'''


# The database clock reads 1000: poste.sh asks psql with the mesh's DSN, vm.sh
# asks the server over ssh. The fake ssh also answers vm.sh's migration check
# (one migration, applied), the unit's ExecStart and its restart.
FAKE_SSH = r'''#!/bin/sh
echo "$0 $*" >> "$FAKE_STATE/argv.log"
for last; do :; done
case $last in
  *clock_timestamp*) echo 1000 ;;
  *"rev-parse HEAD"*) echo abc123 ;;
  *ls-tree*) echo src/ameesh/migrations/0001_init.sql ;;
  *psql*) echo 1 ;;
  *"systemctl show"*)
    argv="/x run --agents dev9 --runner-id runner-dev9@host"
    [ -e "$FAKE_STATE/execstart" ] && argv=$(cat "$FAKE_STATE/execstart")
    [ "$argv" = - ] || echo "{ path=/x ; argv[]=$argv ; }" ;;
  *"systemctl restart"*) : > "$FAKE_STATE/restarted" ;;
esac
'''

# The database clock read on the workstation: through ameesh's own connection
# (the venv's python), never psql with the DSN; both are logged.
FAKE_LOGGED_CLOCK = '#!/bin/sh\necho "$0 $*" >> "$FAKE_STATE/argv.log"\necho 1000\n'


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
        with open(os.path.join(conf, "config.json"), "w", encoding="utf-8") as fh:
            fh.write('{"dsn": "postgresql://ameesh:%s@mesh.invalid/ameesh"}\n' % PASSWORD)
        for path, text in ((os.path.join(bin_dir, "systemctl"), FAKE_SYSTEMCTL),
                           (os.path.join(bin_dir, "sleep"), "#!/bin/sh\nexit 0\n"),
                           (os.path.join(bin_dir, "ssh"), FAKE_SSH),
                           (os.path.join(bin_dir, "psql"), FAKE_LOGGED_CLOCK),
                           (os.path.join(venv_bin, "python"), FAKE_LOGGED_CLOCK),
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

    def execstart(self, argv):
        with open(os.path.join(self.state, "execstart"), "w", encoding="utf-8") as fh:
            fh.write(argv)

    def test_runner_id_is_read_as_argparse_reads_it(self):
        # the equals form, the last of repeated options, an abbreviation: the
        # lease held before is recognised, so a runner that does not take it
        # again fails the restart instead of passing with a WARNING
        for argv in ("/x run --agents dev9 --runner-id=runner-dev9@host",
                     "/x run --agents dev9 --runner-id other@x --runner-id runner-dev9@host",
                     "/x run --agents dev9 --runner runner-dev9@host"):
            with self.subTest(argv=argv):
                restarted = os.path.join(self.state, "restarted")
                if os.path.exists(restarted):
                    os.remove(restarted)
                self.execstart(argv)
                proc = self.restart("no-lease")
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("did not take its lease again", proc.stderr)

    def test_unreadable_execstart_stops_before_any_restart(self):
        self.execstart("-")
        proc = self.restart("lease")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.state, "restarted")))

    def test_no_password_on_a_command_line(self):
        proc = self.restart("lease")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.state, "argv.log"), encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("dev9", log)
        self.assertNotIn(PASSWORD, log)

    def test_runner_that_takes_its_lease_again_passes(self):
        proc = self.restart("lease")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dev9 : redémarré", proc.stdout)

    def test_persona_without_a_lease_before_is_not_checked(self):
        proc = self.restart("not-held")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dev9 : redémarré", proc.stdout)
        self.assertIn("no lease before the restart", proc.stdout)

    def test_active_unit_without_its_lease_fails_the_restart(self):
        for scenario in ("no-lease", "stale-lease", "other-owner", "expired"):
            with self.subTest(scenario=scenario):
                restarted = os.path.join(self.state, "restarted")
                if os.path.exists(restarted):   # a failed subtest leaves it behind
                    os.remove(restarted)
                proc = self.restart(scenario)
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIn("dev9", proc.stderr)
                self.assertIn("retour", proc.stderr)
                self.assertNotIn("dev9 : redémarré", proc.stdout)


class VmRestartLeaseTest(RestartLeaseTest):
    def setUp(self):
        super().setUp()
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
