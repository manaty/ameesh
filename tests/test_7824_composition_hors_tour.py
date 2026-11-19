# SPDX-License-Identifier: AGPL-3.0-only
"""#7824 : composition manuelle et recomposition hors du tour.

Deux pertes sur h-10 (#7814) le 18/11 : le gel du composeur désigné refusé
après une reprise du bail de sa session (« epoch 293 in the session, 295 in
the registry »), et la recomposition du pilote morte avec le tour qui l'avait
lancée. Témoins :

- `compose` et `recompose` partent dans une unité `systemd-run --user`
  nommée, l'appelant rend la main avec son nom ; `--attached` compose sur
  place (ce que lance l'unité) ; les refus d'entrée restent immédiats ;
- l'unité ne garde ni le jeton de bail de la session ni les variables du
  tour, et lit l'environnement d'un fichier 0600 qu'elle efface ; un rôle
  élu y compose sous `ameesh role run`, comme le tic ;
- le gel d'un composeur dont la session a repris son bail : refusé avec
  l'environnement de la session, accepté avec celui de l'unité ;
- acceptation : l'appelant tué (groupe de processus et tout) pendant la
  composition, l'unité va jusqu'à la poussée et au gel.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock

from tests.test_3845_paquet_horaire import ph

SESSION_ENV = {
    "AGENT_MAIL_NAME": "mty-amh-impl-01",
    "AMEESH_RUNNER_ID": "runner-ameesh@ameesh-agents-1",
    "AGENT_MESH_RUNNER_ID": "runner-ameesh@ameesh-agents-1",
    "AMEESH_LEASE_EPOCH": "293", "AGENT_MESH_LEASE_EPOCH": "293",
    "AMEESH_TURN_ID": "t-1", "AMEESH_TURN_CONTEXT": "x", "AMEESH_LOT": "7814",
    "AMEESH_REFUSAL_FILE": "/tmp/refus", "AMEESH_TMPDIR": "/tmp/tour",
    "TMPDIR": "/tmp/tour", "AMEESH_DSN": "postgresql://base", "PATH": "/usr/bin:/bin",
}


class Recorder:
    def __init__(self, rc=0):
        self.calls: list[list[str]] = []
        self.rc = rc

    def run(self, cmd, **_kw):
        self.calls.append(list(cmd))
        return ph.Result(self.rc, "", "" if self.rc == 0 else "Failed to connect to bus")


def context(repo, shell, dry_run=False):
    return types.SimpleNamespace(repo=pathlib.Path(repo), shell=shell, dry_run=dry_run)


class DetachTest(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="paquet-7824-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.shell = Recorder()

    def detach(self, env=None, elected=False, **kw):
        return ph.detach(context(self.home, self.shell), ["recompose", "--carrier", "7814"],
                         kind="recompose", environ=dict(env or SESSION_ENV),
                         home=self.home, elected=lambda _name: elected, **kw)

    def env_file(self):
        cmd = self.shell.calls[-1]
        return pathlib.Path(cmd[cmd.index("--environ-file") + 1])

    def test_unite_nommee_et_rendue_a_l_appelant(self):
        said = self.detach(wait=1200)
        cmd = self.shell.calls[-1]
        self.assertEqual(cmd[:3], ["systemd-run", "--user", "--unit"])
        unit = cmd[3]
        self.assertTrue(unit.startswith("ameesh-paquet-horaire-recompose-"), unit)
        self.assertIn(unit, said)
        self.assertIn("journalctl --user -u %s" % unit, said)
        self.assertIn("RuntimeMaxSec=%d" % (ph.UNIT_RUNTIME + 1200), cmd)
        self.assertEqual(cmd[-4:], ["recompose", "--carrier", "7814", "--attached"])
        self.assertNotIn("--as-role", cmd)

    def test_environnement_sans_jeton_de_session_par_fichier_0600(self):
        self.detach()
        path = self.env_file()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        env = json.loads(path.read_text(encoding="utf-8"))
        for key in ph.SESSION_LEASE_ENV + ("AMEESH_TURN_ID", "AMEESH_TURN_CONTEXT",
                                           "AMEESH_LOT", "AMEESH_REFUSAL_FILE",
                                           "AMEESH_TMPDIR"):
            self.assertNotIn(key, env)
        self.assertEqual(env["AGENT_MAIL_NAME"], "mty-amh-impl-01")
        self.assertEqual(env["AMEESH_DSN"], "postgresql://base")
        self.assertEqual(env["TMPDIR"], str(self.home / ".cache/ameesh/paquet-auto"))
        # aucune valeur sur la ligne de commande de l'unité
        self.assertNotIn("postgresql://base", " ".join(self.shell.calls[-1]))

    def test_role_elu_compose_sous_son_bail_de_role(self):
        env = dict(SESSION_ENV, AGENT_MAIL_NAME="mesh-design")
        said = self.detach(env, elected=True)
        cmd = self.shell.calls[-1]
        self.assertEqual(cmd[cmd.index("--as-role") + 1], "mesh-design")
        self.assertIn("rôle mesh-design", said)

    def test_commande_de_role_garde_son_epoque(self):
        env = dict(SESSION_ENV, AMEESH_ROLE="mesh-design", AMEESH_LEASE_EPOCH="41")
        env.pop("AGENT_MAIL_NAME")
        self.detach(env, elected=True)
        self.assertNotIn("--as-role", self.shell.calls[-1])
        kept = json.loads(self.env_file().read_text(encoding="utf-8"))
        self.assertEqual(kept["AMEESH_LEASE_EPOCH"], "41")

    def test_echec_de_systemd_run_dit_et_efface_le_fichier(self):
        self.shell.rc = 1
        with self.assertRaisesRegex(ph.HoraireError, "non lancée.*bus"):
            self.detach()
        self.assertFalse(self.env_file().exists())

    def test_essai_ne_lance_rien(self):
        said = ph.detach(context(self.home, self.shell, dry_run=True), ["compose"],
                         kind="compose", environ=SESSION_ENV, home=self.home,
                         elected=lambda _n: False)
        self.assertIn("[essai]", said)
        self.assertEqual(self.shell.calls, [])

    def test_l_unite_lit_puis_efface_le_fichier(self):
        path = self.home / "env.json"
        path.write_text(json.dumps({"AGENT_MAIL_NAME": "a", "PATH": "/bin"}))
        with mock.patch.dict(os.environ, {"AMEESH_LEASE_EPOCH": "293"}, clear=False):
            ph.load_environ(str(path))
            self.assertEqual(os.environ.get("AGENT_MAIL_NAME"), "a")
            self.assertNotIn("AMEESH_LEASE_EPOCH", os.environ)
        self.assertFalse(path.exists())


class MainTest(unittest.TestCase):
    def run_main(self, argv, **patches):
        clone = tempfile.TemporaryDirectory(prefix="paquet-7824-")
        self.addCleanup(clone.cleanup)
        (pathlib.Path(clone.name) / ".git").mkdir()
        argv = [clone.name if a == "CLONE" else a for a in argv]
        zone = mock.Mock(timezone="Europe/Paris")
        with mock.patch.object(ph, "pose_git_identity"), \
                mock.patch.object(ph, "check_fresh"), mock.patch.object(ph, "align_schema"), \
                mock.patch.object(ph, "refuse_agent_clone"), \
                mock.patch("ameesh.config.load", return_value=None), \
                mock.patch("ameesh.mechanics.configured", return_value=zone), \
                mock.patch.object(ph, "detach", return_value="unité") as detached, \
                mock.patch.object(ph, "pilot_recompose", return_value="ok") as recomposed, \
                mock.patch.object(ph, "run", return_value="tic") as ticked:
            rc = ph.main(argv)
        return rc, detached, recomposed, ticked

    def test_recompose_detache_par_defaut(self):
        rc, detached, recomposed, _ = self.run_main(
            ["recompose", "--repo", "CLONE", "--carrier", "7814", "--exclude", "7784",
             "--wait", "20m"])
        self.assertEqual(rc, 0)
        recomposed.assert_not_called()
        sub_argv = detached.call_args.args[1]
        self.assertEqual((sub_argv[0], sub_argv[-2:]), ("recompose", ["--wait", "20m"]))
        self.assertEqual(detached.call_args.kwargs, {"kind": "recompose", "wait": 1200.0})

    def test_refus_d_entree_avant_l_unite(self):
        rc, detached, _, _ = self.run_main(
            ["recompose", "--repo", "CLONE", "--carrier", "7814", "--exclude", "7784",
             "--cancel-ci"])
        self.assertEqual(rc, 2)
        detached.assert_not_called()

    def test_compose_detache_puis_un_tic_attache(self):
        rc, detached, _, ticked = self.run_main(["compose", "--repo", "CLONE"])
        self.assertEqual((rc, detached.call_args.kwargs["kind"]), (0, "compose"))
        ticked.assert_not_called()
        rc, detached, _, ticked = self.run_main(["compose", "--repo", "CLONE", "--attached"])
        self.assertEqual(rc, 0)
        detached.assert_not_called()
        ticked.assert_called_once()

    def test_as_role_relance_sous_role_run_sans_options_d_unite(self):
        clone = tempfile.mkdtemp(prefix="paquet-7824-")
        self.addCleanup(shutil.rmtree, clone, True)
        path = pathlib.Path(clone) / "env.json"
        path.write_text(json.dumps(dict(os.environ)))
        with mock.patch.object(ph.os, "execvp", side_effect=SystemExit(0)) as execvp:
            with self.assertRaises(SystemExit):
                ph.main(["--environ-file", str(path), "--as-role", "mesh-design",
                         "recompose", "--repo", clone, "--carrier", "7814", "--attached"])
        cmd = execvp.call_args.args[1]
        self.assertEqual(cmd[1:5], ["role", "run", "mesh-design", "--"])
        self.assertEqual(cmd[7:], ["recompose", "--repo", clone, "--carrier", "7814",
                                   "--attached"])
        self.assertFalse(path.exists())


class FreezeAfterLeaseTakeoverTest(unittest.TestCase):
    """(2) : le gel ne dépend plus de l'époque de la session de l'appelant."""

    def actor(self, env):
        from ameesh import identity, registry, work
        row = {"name": "mty-amh-impl-01", "lease_epoch": 295, "lease_owner":
               "runner-ameesh@ameesh-agents-1", "live": True}

        def lease_agent(_db, _name, _owner, epoch):
            if int(epoch) != 295:
                return False, "epoch 295 in the registry, %s in the session" % epoch, row
            return True, "", row

        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(registry, "lease_agent", side_effect=lease_agent), \
                mock.patch.object(identity, "_session_binding",
                                  return_value=identity.Binding(reason="unbound")), \
                mock.patch("ameesh.agent_names.resolve", side_effect=lambda n, **_k: n):
            return work.milestone_actor(None, object(), None, kind="frozen")

    def test_session_reprise_refusee_unite_acceptee(self):
        from ameesh import work
        with self.assertRaisesRegex(work.WorkError, "invalid session identity.*epoch 295"):
            self.actor(SESSION_ENV)
        unit = ph.unit_environment(SESSION_ENV, pathlib.Path("/home/x"))
        self.assertEqual(self.actor(unit), "mty-amh-impl-01")


STUB = textwrap.dedent('''
    import json, os, sys, time
    path = sys.argv[sys.argv.index("--environ-file") + 1]
    env = json.load(open(path))
    os.unlink(path)
    os.environ.clear(); os.environ.update(env)
    marks = os.environ["PAQUET_7824_MARKS"]
    open(marks + "/start", "w").write(str(os.getpid()))
    time.sleep(3)
    open(marks + "/push", "w").write("poussée")
    open(marks + "/freeze", "w").write(json.dumps(
        {k: os.environ.get(k) for k in ("AGENT_MAIL_NAME", "AMEESH_LEASE_EPOCH")}))
''')

CALLER = textwrap.dedent('''
    import pathlib, sys, time, types
    sys.path.insert(0, sys.argv[1])
    from tests.test_3845_paquet_horaire import ph
    home = pathlib.Path(sys.argv[2])
    ctx = types.SimpleNamespace(repo=home, shell=ph.Shell(), dry_run=False)
    print(ph.detach(ctx, ["compose"], kind="compose", home=home,
                    elected=lambda _n: False, script=pathlib.Path(sys.argv[3])), flush=True)
    time.sleep(60)
''')


def user_manager() -> bool:
    if not shutil.which("systemd-run"):
        return False
    probe = subprocess.run(["systemd-run", "--user", "--quiet", "--collect", "--wait",
                            "/bin/true"], capture_output=True, timeout=30)
    return probe.returncode == 0


class CallerKilledTest(unittest.TestCase):
    """Acceptation : l'appelant tué, la composition va à la poussée et au gel."""

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        if proc.stdout and not proc.stdout.closed:
            proc.stdout.close()

    def test_appelant_tue_pendant_la_composition(self):
        if not user_manager():
            self.skipTest("gestionnaire systemd utilisateur indisponible (non concluant)")
        home = pathlib.Path(tempfile.mkdtemp(prefix="paquet-7824-"))
        self.addCleanup(shutil.rmtree, home, True)
        marks = home / "marks"
        marks.mkdir()
        stub = home / "stub.py"
        stub.write_text(STUB)
        caller = home / "caller.py"
        caller.write_text(CALLER)
        env = dict(os.environ, **{k: v for k, v in SESSION_ENV.items()
                                  if k not in ("PATH", "TMPDIR", "AMEESH_DSN")})
        env["PAQUET_7824_MARKS"] = str(marks)
        root = str(pathlib.Path(__file__).resolve().parent.parent)
        # un tour : sa propre session, tuée en groupe comme l'exécuteur le fait
        proc = subprocess.Popen([sys.executable, str(caller), root, str(home), str(stub)],
                                env=env, stdout=subprocess.PIPE, text=True,
                                start_new_session=True)
        self.addCleanup(self.stop, proc)
        said = proc.stdout.readline()
        self.assertIn("ameesh-paquet-horaire-compose-", said)
        deadline = time.time() + 20
        while not (marks / "start").exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue((marks / "start").exists(), "unité non démarrée")
        self.stop(proc)
        self.assertFalse((marks / "push").exists(), "composition finie avant le kill")
        deadline = time.time() + 20
        while not (marks / "freeze").exists() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue((marks / "push").exists())
        frozen = json.loads((marks / "freeze").read_text())
        self.assertEqual(frozen, {"AGENT_MAIL_NAME": "mty-amh-impl-01",
                                  "AMEESH_LEASE_EPOCH": None})
        self.assertEqual(list((home / ".cache/ameesh/paquet-auto/units").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
