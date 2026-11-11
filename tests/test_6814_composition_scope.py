# SPDX-License-Identifier: AGPL-3.0-only
"""#6814 — la composition d'un agent vit hors du cgroup de son exécuteur.

Le banc n'appelle pas systemd : il vérifie la décision (cgroup d'un runner ou
non), l'environnement et le journal du scope (le bail du tour tombe, le nom de
l'agent reste, le lot suit le porteur, les sorties ne passent plus par le tube
du tour), la ligne `systemd-run --scope` et la session neuve du scope (B/P1
impl-11 : `killpg` du groupe du tour ne doit pas l'atteindre), sans jamais
créer d'unité réelle.

Contexte : la mise à jour staging du 11/11/2026 à 01:42:52Z a arrêté
l'exécuteur d'AG1 et tué son cgroup ; la composition de h-03 (#6810), lancée
dans le tour de mty-amh-impl-01, est morte sous son bail sans pousser.
"""
from __future__ import annotations

import importlib.util
import io
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "paquet-auto.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("test_6814_paquet_auto", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_6814_paquet_auto"] = module
    spec.loader.exec_module(module)
    return module


paquet_auto = _load_script()

RUNNER = ("0::/user.slice/user-1001.slice/user@1001.service/ameesh.slice/"
          "ameesh-agents.slice/ameesh-runner-agent@mty-amh-impl-01.service\n")


class InRunnerCgroup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="6814-cgroup-")
        self.addCleanup(self.tmp.cleanup)

    def _write(self, content: str) -> str:
        path = pathlib.Path(self.tmp.name) / "cgroup"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_agent_runner_unit(self) -> None:
        self.assertTrue(paquet_auto.in_runner_cgroup(self._write(RUNNER)))

    def test_runner_unit_without_agent_suffix(self) -> None:
        text = "0::/user.slice/user-1001.slice/user@1001.service/ameesh-runner@x.service\n"
        self.assertTrue(paquet_auto.in_runner_cgroup(self._write(text)))

    def test_child_of_the_runner_unit(self) -> None:
        text = RUNNER.replace("\n", "/turn-abc\n")
        self.assertTrue(paquet_auto.in_runner_cgroup(self._write(text)))

    def test_timer_unit_is_not_a_turn(self) -> None:
        text = ("0::/user.slice/user-1001.slice/user@1001.service/"
                "ameesh-paquet-horaire.service\n")
        self.assertFalse(paquet_auto.in_runner_cgroup(self._write(text)))

    def test_dsh_scope_is_already_outside(self) -> None:
        text = ("0::/user.slice/user-1001.slice/user@1001.service/app.slice/"
                "dsh-subprocess-42-72deff11d18c.scope\n")
        self.assertFalse(paquet_auto.in_runner_cgroup(self._write(text)))

    def test_missing_or_empty_file_is_not_a_turn(self) -> None:
        self.assertFalse(paquet_auto.in_runner_cgroup(
            str(pathlib.Path(self.tmp.name) / "absent")))
        self.assertFalse(paquet_auto.in_runner_cgroup(self._write("0::/\n")))


class ScopeEnvironment(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="6814-env-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / "paquet-auto"
        self.patch = mock.patch.object(paquet_auto, "SCOPE_TMPDIR", str(self.root))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.turn = {
            "AGENT_MAIL_NAME": "mty-amh-impl-01",
            "AMEESH_DSN": "user=ameesh dbname=ameesh host=/run/socket",
            "AMEESH_CONFIG": "/home/ameesh/.config/ameesh/config.json",
            "AMEESH_RUNNER_ID": "runner-mty-amh-impl-01@ameesh-agents-1",
            "AGENT_MESH_RUNNER_ID": "runner-mty-amh-impl-01@ameesh-agents-1",
            "AMEESH_LEASE_EPOCH": "249",
            "AGENT_MESH_LEASE_EPOCH": "249",
            "AMEESH_LOT": "6814",
            "AMEESH_TURN_ID": "turn-h-03",
            "AMEESH_CONTAINER_LABELS": "ameesh.turn=turn-h-03,ameesh.agent=mty-amh-impl-01,"
                                       "ameesh.lot=6814",
            "TMPDIR": "/home/ameesh/.local/state/ameesh/.menage/tmp/impl-01~abc",
        }
        self.environ = mock.patch.dict(os.environ, self.turn, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def test_bail_du_tour_tombe_et_l_agent_reste(self) -> None:
        env = paquet_auto.scope_environment(carrier=6810)
        self.assertEqual(env[paquet_auto.SCOPED_ENV], "1")
        for variable in ("AMEESH_RUNNER_ID", "AGENT_MESH_RUNNER_ID",
                         "AMEESH_LEASE_EPOCH", "AGENT_MESH_LEASE_EPOCH"):
            self.assertNotIn(variable, env)
        self.assertEqual(env["AGENT_MAIL_NAME"], "mty-amh-impl-01")
        self.assertEqual(env["AMEESH_DSN"], self.turn["AMEESH_DSN"])
        self.assertEqual(env["AMEESH_CONFIG"], self.turn["AMEESH_CONFIG"])
        self.assertEqual(env["AMEESH_TURN_ID"], "turn-h-03")
        # le lot du tour ne suit pas : le porteur est le sujet de la composition
        self.assertEqual(env["AMEESH_LOT"], "6810")
        self.assertEqual(env["AMEESH_CONTAINER_LABELS"],
                         "ameesh.turn=turn-h-03,ameesh.agent=mty-amh-impl-01,"
                         "ameesh.lot=6810")

    def test_tmpdir_stable_et_journal(self) -> None:
        log = pathlib.Path(self.tmp.name) / "journal.log"
        env = paquet_auto.scope_environment(carrier=6810, log=log)
        for variable in ("TMPDIR", "TMP", "TEMP", "AMEESH_TMPDIR"):
            self.assertEqual(env[variable], str(self.root))
        self.assertEqual(env[paquet_auto.SCOPE_LOG_ENV], str(log))
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)

    def test_sans_porteur_le_lot_du_tour_ne_suit_pas(self) -> None:
        env = paquet_auto.scope_environment()
        self.assertNotIn("AMEESH_LOT", env)
        self.assertEqual(env["AMEESH_CONTAINER_LABELS"],
                         self.turn["AMEESH_CONTAINER_LABELS"])

    def test_l_environnement_du_processus_est_intact(self) -> None:
        paquet_auto.scope_environment(carrier=6810)
        self.assertEqual(os.environ["AMEESH_LEASE_EPOCH"], "249")
        self.assertEqual(os.environ["AMEESH_LOT"], "6814")


class ScopeLog(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="6814-log-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / "paquet-auto"
        self.patch = mock.patch.object(paquet_auto, "SCOPE_TMPDIR", str(self.root))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_journal_sous_le_dossier_stable(self) -> None:
        log = paquet_auto.scope_log("ameesh-paquet-auto-h-x-1-abcd1234")
        self.assertEqual(log, self.root / "logs" / "ameesh-paquet-auto-h-x-1-abcd1234.log")
        self.assertEqual(stat.S_IMODE(log.parent.stat().st_mode), 0o700)

    def test_copie_incrementale_du_journal(self) -> None:
        log = pathlib.Path(self.tmp.name) / "journal.log"
        log.write_bytes(b"premier\n")
        out = types.SimpleNamespace(buffer=io.BytesIO())
        with mock.patch.object(sys, "stdout", out):
            offset = paquet_auto._copy_log(log, 0)
            self.assertEqual(offset, len(b"premier\n"))
            self.assertEqual(out.buffer.getvalue(), b"premier\n")
            with log.open("ab") as stream:
                stream.write(b"second\n")
            offset = paquet_auto._copy_log(log, offset)
        self.assertEqual(out.buffer.getvalue(), b"premier\nsecond\n")
        self.assertEqual(offset, len(b"premier\nsecond\n"))

    def test_lit_aussi_une_sortie_sans_buffer(self) -> None:
        log = pathlib.Path(self.tmp.name) / "journal.log"
        log.write_bytes("écrit".encode("utf-8"))
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            paquet_auto._copy_log(log, 0)
        self.assertEqual(out.getvalue(), "écrit")


class ScopeCommand(unittest.TestCase):
    def test_redirige_les_deux_sorties_vers_le_journal(self) -> None:
        argv = ["--name", "h-x", "--carrier", "42"]
        command = paquet_auto.scope_command(
            argv, unit="ameesh-paquet-auto-h-x-1-abcd1234",
            binary="/usr/bin/systemd-run")
        self.assertEqual(command[:6], [
            "/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect",
            "--unit"])
        self.assertEqual(command[6], "ameesh-paquet-auto-h-x-1-abcd1234")
        self.assertEqual(command[7:9], ["/bin/sh", "-c"])
        self.assertIn('>>"$%s" 2>&1' % paquet_auto.SCOPE_LOG_ENV, command[9])
        self.assertEqual(command[10:12], [sys.executable, str(SCRIPT.resolve())])
        self.assertEqual(command[12:], argv)


class MaybeScope(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="6814-scope-")
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / "paquet-auto"
        self.patch = mock.patch.object(paquet_auto, "SCOPE_TMPDIR", str(self.root))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.environ = mock.patch.dict(os.environ, {
            "AGENT_MAIL_NAME": "mty-amh-impl-01",
            "AMEESH_RUNNER_ID": "runner-mty-amh-impl-01@ameesh-agents-1",
            "AMEESH_LEASE_EPOCH": "249",
            "AMEESH_LOT": "6814",
        }, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def test_hors_runner_aucun_scope(self) -> None:
        with mock.patch.object(paquet_auto, "in_runner_cgroup", return_value=False), \
                mock.patch.object(paquet_auto.subprocess, "Popen") as popen:
            self.assertIsNone(paquet_auto.maybe_scope(
                ["--name", "h-x", "--carrier", "42"], name="h-x", carrier=42))
        popen.assert_not_called()

    def test_scope_deja_pose_ne_se_repete_pas(self) -> None:
        os.environ[paquet_auto.SCOPED_ENV] = "1"
        with mock.patch.object(paquet_auto, "in_runner_cgroup", return_value=True), \
                mock.patch.object(paquet_auto.subprocess, "Popen") as popen:
            self.assertIsNone(paquet_auto.maybe_scope(
                ["--name", "h-x", "--carrier", "42"], name="h-x", carrier=42))
        popen.assert_not_called()

    def test_systemd_run_absent_replie_et_le_dit(self) -> None:
        with mock.patch.object(paquet_auto, "in_runner_cgroup", return_value=True), \
                mock.patch.object(paquet_auto.shutil, "which", return_value=None), \
                mock.patch.object(paquet_auto.subprocess, "Popen") as popen, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertIsNone(paquet_auto.maybe_scope(
                ["--name", "h-x", "--carrier", "42"], name="h-x", carrier=42))
        popen.assert_not_called()
        self.assertIn("systemd-run absent", err.getvalue())
        self.assertIn("redémarrage staging la tuerait", err.getvalue())

    def test_lance_le_scope_et_recopie_le_journal(self) -> None:
        argv = ["--name", "h-x", "--carrier", "42", "--target", "main"]
        proc = mock.Mock()
        proc.poll.return_value = 0
        with mock.patch.object(paquet_auto, "in_runner_cgroup", return_value=True), \
                mock.patch.object(paquet_auto.shutil, "which",
                                  return_value="/usr/bin/systemd-run"), \
                mock.patch.object(paquet_auto.subprocess, "Popen",
                                  return_value=proc) as popen, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = paquet_auto.maybe_scope(argv, name="h-x", carrier=42)
        self.assertEqual(code, 0)
        popen.assert_called_once()
        command, = popen.call_args.args
        kwargs = popen.call_args.kwargs
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertRegex(command[6], r"^ameesh-paquet-auto-h-x-\d+-[0-9a-f]{8}$")
        self.assertEqual(command[12:], argv)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        # B/P1 impl-11 : le scope doit avoir sa session, sinon `killpg` du
        # groupe du tour (runner.py, `_signal_group`) l'atteint encore.
        self.assertIs(kwargs["start_new_session"], True)
        env = kwargs["env"]
        self.assertEqual(env[paquet_auto.SCOPED_ENV], "1")
        for variable in ("AMEESH_RUNNER_ID", "AMEESH_LEASE_EPOCH"):
            self.assertNotIn(variable, env)
        self.assertEqual(env["AMEESH_LOT"], "42")
        log = pathlib.Path(env[paquet_auto.SCOPE_LOG_ENV])
        self.assertEqual(log.parent, self.root / "logs")
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertIn("composition hors du cgroup de l'exécuteur", err.getvalue())
        self.assertIn(str(log), err.getvalue())

    def test_erreur_du_scope_replie_et_le_dit(self) -> None:
        with mock.patch.object(paquet_auto, "in_runner_cgroup", return_value=True), \
                mock.patch.object(paquet_auto.shutil, "which",
                                  return_value="/usr/bin/systemd-run"), \
                mock.patch.object(paquet_auto.subprocess, "Popen",
                                  side_effect=OSError("no bus")), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertIsNone(paquet_auto.maybe_scope(
                ["--name", "h-x", "--carrier", "42"], name="h-x", carrier=42))
        self.assertIn("scope indisponible (no bus)", err.getvalue())
        self.assertIn("redémarrage staging la tuerait", err.getvalue())

    def test_suit_le_scope_jusqu_a_son_code(self) -> None:
        log = pathlib.Path(self.tmp.name) / "journal.log"
        log.write_bytes(b"hello\n")
        proc = mock.Mock()
        proc.poll.side_effect = [None, 0]
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out), \
                mock.patch.object(paquet_auto.time, "sleep"):
            self.assertEqual(paquet_auto._follow_scope(proc, log), 0)
        self.assertEqual(out.getvalue(), "hello\n")


class EntryPoint(unittest.TestCase):
    def test_l_import_du_module_ne_reexecute_pas(self) -> None:
        """Les tests et `paquet-horaire` importent le module : `main` ne doit
        pas remplacer le processus de l'appelant (#6814)."""
        self.assertNotEqual(paquet_auto.__name__, "__main__")
        with mock.patch.object(paquet_auto, "maybe_scope") as scope:
            code = paquet_auto.main(["--name", "h-x"])
        self.assertEqual(code, 2)
        scope.assert_not_called()

    def test_le_point_d_entree_rend_le_code_du_scope(self) -> None:
        with mock.patch.object(paquet_auto, "__name__", "__main__"), \
                mock.patch.object(paquet_auto, "maybe_scope", return_value=7) as scope:
            code = paquet_auto.main(["--name", "h-x", "--carrier", "42"])
        self.assertEqual(code, 7)
        scope.assert_called_once_with(["--name", "h-x", "--carrier", "42"],
                                      name="h-x", carrier=42)

    def test_sans_scope_le_point_d_entree_continue(self) -> None:
        with mock.patch.object(paquet_auto, "__name__", "__main__"), \
                mock.patch.object(paquet_auto, "maybe_scope", return_value=None):
            code = paquet_auto.main(["--name", "h-x"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
