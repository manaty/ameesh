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
unit = args[-1]
with open(os.path.join(state, "argv.log"), "a") as fh:
    fh.write(" ".join(sys.argv) + "\n")

def etat():
    """(ActiveState, MainPID) de l'unité : vivante par défaut — un runner au
    repos est un processus vivant ; inactive après un stop prouvé."""
    path = os.path.join(state, "unit")
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().split()
    return ["active", "1234"]

def scenario():
    path = os.path.join(state, "stop_mode")
    return open(path).read().strip() if os.path.exists(path) else ""

def operation(op):
    with open(os.path.join(state, "ops.log"), "a") as fh:
        fh.write("%s %s\n" % (op, unit))

if args[0] == "list-units":
    print("ameesh-runner-agent@dev9.service loaded active running runner")
elif args[0] == "show":
    prop = args[args.index("-p") + 1] if "-p" in args else ""
    if prop == "ActiveState":
        operation("show ActiveState")
        print(etat()[0])
    elif prop == "MainPID":
        operation("show MainPID")
        print(etat()[1])
    else:
        operation("show ExecStart")
        path = os.path.join(state, "execstart")
        argv = (open(path).read().strip() if os.path.exists(path)
                else "/x run --agents dev9 --runner-id runner-dev9@host")
        if argv != "-":   # "-": ExecStart unreadable
            print("{ path=/x ; argv[]=%s ; }" % argv)
elif args[0] == "stop":
    operation("stop")
    if scenario() == "stop-fails":
        sys.exit(1)
    if scenario() == "still-active":
        sys.exit(0)       # l'unité n'a pas atteint le point sûr
    vivante = etat()
    with open(os.path.join(state, "drained"), "a") as fh:
        fh.write("stop active=%s pid=%s\n" % tuple(vivante))
    if scenario() == "pid-linger":
        final = ["inactive", "1234"]
    elif scenario() == "failed":
        final = ["failed", "0"]   # stop non abouti (délai -> SIGKILL, sortie en erreur)
    else:
        final = ["inactive", "0"]
    with open(os.path.join(state, "unit"), "w") as fh:
        fh.write(" ".join(final) + "\n")
elif args[0] == "start":
    operation("start")
    with open(os.path.join(state, "restarted"), "w") as fh:
        fh.write(str(time.time()))
    with open(os.path.join(state, "unit"), "w") as fh:
        fh.write("active 4321\n")
elif args[0] == "restart":
    # ancien chemin (interdit par #1152) : tracé pour que le témoin le refuse
    operation("restart")
    with open(os.path.join(state, "restarted"), "w") as fh:
        fh.write(str(time.time()))
    with open(os.path.join(state, "unit"), "w") as fh:
        fh.write("active 4321\n")
elif args[0] == "is-active":
    operation("is-active")
    # le marqueur global ne vise que l'unité du runner (#1633) ; l'état
    # unitaire vient de l'arrêt/démarrage prouvés (#1152).
    if "ameesh-runner" in " ".join(args) \
            and os.path.exists(os.path.join(state, "unit-inactive")):
        sys.exit(3)
    if etat()[0] == "active":
        print("active")
    else:
        print("inactive")
        sys.exit(3)
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
# #1152/R1 : le statut peut rester `running` à chaque lecture (incident
# impl07) ; le fichier `statut_force` le fige pour le témoin de starvation.
statut=idle
[ -e "$FAKE_STATE/statut_force" ] && statut=$(cat "$FAKE_STATE/statut_force")
owner='"runner-dev9@host"' epoch=7 expires=1300
[ "$scenario" = not-held ] && owner=null
[ "$scenario" = stopped ] && statut=stopped
if [ -e "$FAKE_STATE/restarted" ]; then
  case $scenario in
    lease) epoch=9 ;;
    no-lease) owner=null epoch=8 ;;
    other-owner) owner='"attach-dev9@elsewhere"' epoch=9 ;;
    expired) epoch=9 expires=900 ;;
  esac
fi
printf '{"name": "dev9", "status": "%s", "lease_owner": %s, "lease_epoch": %s, "lease_expires_ts": %s}\n' \
  "$statut" "$owner" "$epoch" "$expires"
'''


# The database clock reads 1000: poste.sh asks psql with the mesh's DSN, vm.sh
# asks the server over ssh. The fake ssh also answers vm.sh's migration check
# (one migration, applied), the unit's ExecStart, its stop/proof/start
# sequence (#1152) and its state.
FAKE_SSH = r'''#!/bin/sh
echo "$0 $*" >> "$FAKE_STATE/argv.log"
for last; do :; done
unite=${last##* }
etat() {
  if [ -e "$FAKE_STATE/unit" ]; then cat "$FAKE_STATE/unit"; else echo "active 1234"; fi
}
stop_mode() {
  if [ -e "$FAKE_STATE/stop_mode" ]; then cat "$FAKE_STATE/stop_mode"; else echo ""; fi
}
case $last in
  *clock_timestamp*) echo 1000 ;;
  *"rev-parse HEAD"*) echo abc123 ;;
  *ls-tree*) echo src/ameesh/migrations/0001_init.sql ;;
  *psql*) echo 1 ;;
  *"ActiveState"*)
    echo "show ActiveState $unite" >> "$FAKE_STATE/ops.log"
    set -- $(etat); echo "$1" ;;
  *"MainPID"*)
    echo "show MainPID $unite" >> "$FAKE_STATE/ops.log"
    set -- $(etat); echo "$2" ;;
  *"systemctl show"*)
    echo "show ExecStart $unite" >> "$FAKE_STATE/ops.log"
    argv="/x run --agents dev9 --runner-id runner-dev9@host"
    [ -e "$FAKE_STATE/execstart" ] && argv=$(cat "$FAKE_STATE/execstart")
    [ "$argv" = - ] || echo "{ path=/x ; argv[]=$argv ; }" ;;
  *"systemctl stop"*)
    echo "stop $unite" >> "$FAKE_STATE/ops.log"
    [ "$(stop_mode)" = "stop-fails" ] && exit 1
    [ "$(stop_mode)" = "still-active" ] && exit 0
    set -- $(etat)
    echo "stop active=$1 pid=$2" >> "$FAKE_STATE/drained"
    case $(stop_mode) in
      pid-linger) echo "inactive 1234" > "$FAKE_STATE/unit" ;;
      failed) echo "failed 0" > "$FAKE_STATE/unit" ;;
      *) echo "inactive 0" > "$FAKE_STATE/unit" ;;
    esac ;;
  *"systemctl start"*)
    echo "start $unite" >> "$FAKE_STATE/ops.log"
    : > "$FAKE_STATE/restarted"
    echo "active 4321" > "$FAKE_STATE/unit" ;;
  *"systemctl restart"*)
    echo "restart $unite" >> "$FAKE_STATE/ops.log"
    : > "$FAKE_STATE/restarted"
    echo "active 4321" > "$FAKE_STATE/unit" ;;
  *"systemctl is-active"*)
    echo "is-active $unite" >> "$FAKE_STATE/ops.log"
    # le marqueur global ne vise que l'unité du runner (#1633) ; l'état
    # unitaire vient de l'arrêt/démarrage prouvés (#1152).
    if [ -e "$FAKE_STATE/unit-inactive" ] && [ "${last#*ameesh-runner}" != "$last" ]; then
      exit 3
    fi
    set -- $(etat)
    if [ "$1" = active ]; then echo active; else echo inactive; exit 3; fi ;;
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

    def ops(self):
        """Opérations systemctl observées, dans l'ordre : `stop`,
        `show ActiveState`, `show MainPID`, `show ExecStart`, `start`,
        `is-active`, `restart` (unité locale ou distante)."""
        path = os.path.join(self.state, "ops.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [tuple(ligne.split()) for ligne in fh.read().splitlines()]

    def op_position(self, nom):
        """Rang de la première opération `nom` sur l'unité de dev9, None si
        elle n'a pas eu lieu."""
        for rang, ligne in enumerate(self.ops()):
            if not any("dev9" in champ for champ in ligne):
                continue
            etiquette = " ".join(ligne[:-1]) if ligne[0] == "show" else ligne[0]
            if etiquette == nom:
                return rang
        return None

    def clear_ops(self):
        for nom in ("restarted", "unit", "ops.log", "drained", "stop_mode"):
            chemin = os.path.join(self.state, nom)
            if os.path.exists(chemin):
                os.remove(chemin)

    def test_le_redemarrage_passe_par_un_arret_prouve(self):
        """#1152 : jamais `restart` — `stop` (SIGTERM : drainage), preuve du
        point sûr (ActiveState=inactive ET MainPID=0), puis `start`. Un tour
        parti entre la lecture du statut et le stop est drainé : l'unité était
        vivante au stop (la course que la lecture de statut ne ferme pas)."""
        proc = self.restart("lease")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dev9 : redémarré", proc.stdout)
        stop = self.op_position("stop")
        actif = self.op_position("show ActiveState")
        pid = self.op_position("show MainPID")
        start = self.op_position("start")
        self.assertIsNotNone(stop, "l'unité est arrêtée avant tout démarrage")
        self.assertIsNotNone(actif, "l'état est relu pour la preuve")
        self.assertIsNotNone(pid, "le PID principal est relu pour la preuve")
        self.assertIsNotNone(start, "l'unité est démarrée après la preuve")
        self.assertLess(stop, actif, "la preuve vient après l'arrêt")
        self.assertLess(actif, start, "le démarrage vient après la preuve")
        self.assertLess(pid, start, "le PID nul est prouvé avant le démarrage")
        self.assertIsNone(self.op_position("restart"),
                          "restart ne prouve pas le point sûr : interdit")
        with open(os.path.join(self.state, "drained"), encoding="utf-8") as fh:
            self.assertIn("pid=1234", fh.read(),
                          "l'unité était vivante au stop : le tour est drainé, pas tué")

    def test_arret_non_prouve_aucun_demarrage(self):
        """#1152 : point sûr non prouvé (unité encore active, MainPID restant,
        état `failed` — drainage non abouti, ou stop en échec) : l'unité n'est
        PAS démarrée, le déploiement échoue en la nommant."""
        for mode in ("still-active", "pid-linger", "failed", "stop-fails"):
            with self.subTest(mode=mode):
                self.clear_ops()
                with open(os.path.join(self.state, "stop_mode"), "w",
                          encoding="utf-8") as fh:
                    fh.write(mode + "\n")
                proc = self.restart("lease")
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertIsNone(self.op_position("start"),
                                  "aucun démarrage sans point sûr prouvé")
                self.assertFalse(os.path.exists(os.path.join(self.state, "restarted")))
                self.assertNotIn("dev9 : redémarré", proc.stdout)
                attendu = ("l'arrêt échoue" if mode == "stop-fails"
                           else "arrêt non prouvé")
                self.assertIn(attendu, proc.stderr)

    def test_statut_running_a_chaque_lecture_ne_bloque_pas(self):
        """#1152/R1 (starvation de l'incident impl07) : un statut `running` à
        chaque lecture n'attend plus une fenêtre « idle » — l'unité est
        arrêtée (SIGTERM, drainage borné), le point sûr est prouvé, puis elle
        est démarrée. Aucune expiration « toujours en tour »."""
        with open(os.path.join(self.state, "statut_force"), "w",
                  encoding="utf-8") as fh:
            fh.write("running\n")
        proc = self.restart("lease")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dev9 : redémarré (était running)", proc.stdout)
        self.assertNotIn("toujours en tour", proc.stderr)
        stop = self.op_position("stop")
        actif = self.op_position("show ActiveState")
        pid = self.op_position("show MainPID")
        start = self.op_position("start")
        for nom, rang in (("stop", stop), ("show ActiveState", actif),
                          ("show MainPID", pid), ("start", start)):
            self.assertIsNotNone(rang, nom)
        self.assertLess(stop, actif)
        self.assertLess(actif, start)
        self.assertLess(pid, start)
        self.assertIsNone(self.op_position("restart"),
                          "restart ne prouve pas le point sûr : interdit")

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

    def test_persona_stopped_with_an_active_unit_is_reloaded_not_reactivated(self):
        # #1131/#1633 : la persona arrêtée par le propriétaire n'est pas
        # réactivée (aucun bail exigé, aucun tour), mais son unité encore
        # active recharge le nouveau code — sinon une reprise ultérieure
        # repartait sur l'ancienne version.
        proc = self.restart("stopped")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.state, "argv.log"), encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("restart ameesh-runner", log)
        self.assertIn("dev9 : arrêté dans le registre", proc.stdout)
        self.assertIn("nouveau code", proc.stdout)
        self.assertNotIn("no lease before the restart", proc.stdout)
        self.assertNotIn("dev9 : redémarré (était", proc.stdout)

    def test_persona_stopped_with_an_inactive_unit_is_not_started(self):
        # #1633 : une unité déjà arrêtée n'est pas relancée — elle chargera le
        # nouveau code à son démarrage.
        with open(os.path.join(self.state, "unit-inactive"), "w", encoding="utf-8"):
            pass
        proc = self.restart("stopped")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.state, "argv.log"), encoding="utf-8") as fh:
            log = fh.read()
        self.assertNotIn("restart ameesh-runner", log)
        self.assertIn("déjà arrêtée", proc.stdout)

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
