# SPDX-License-Identifier: AGPL-3.0-only
"""#8123 (manaty/ameesh#6356) : attach local, taille du terminal et configuration.

Constat : le harnais attaché vit dans sa propre session, sans terminal de
contrôle ; le noyau envoie SIGWINCH au groupe de premier plan (l'attach), et
le harnais dessinait pour la taille lue au départ. Dans la même session, son
`AMEESH_CONFIG` hérité nommait un fichier disparu : chaque hook `agent-mail`
échouait. L'attach relaie désormais SIGWINCH au groupe du harnais et lui
transmet le fichier de configuration qu'il a lui-même chargé (refus explicite
si ce fichier a disparu).
"""
from __future__ import annotations

import dataclasses
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ameesh.config import Config
from ameesh.runner import _attach_config_env, _WinchRelay, run_attach

from .support import FAKEBIN, PgTestCase


class WinchRelayTest(unittest.TestCase):
    def test_signal_reaches_the_harness_group_then_handler_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            recu = os.path.join(directory, "winch")
            pret = os.path.join(directory, "pret")
            script = ("import os, signal, sys, time\n"
                      "signal.signal(signal.SIGWINCH, lambda *_: open(sys.argv[1], 'w').close())\n"
                      "open(sys.argv[2], 'w').close()\n"
                      "end = time.monotonic() + 10\n"
                      "while not os.path.exists(sys.argv[1]) and time.monotonic() < end:\n"
                      "    time.sleep(0.02)\n")
            avant = signal.getsignal(signal.SIGWINCH)
            relais = _WinchRelay().arm()
            proc = subprocess.Popen([sys.executable, "-c", script, recu, pret],
                                    start_new_session=True)
            try:
                relais.pgid = proc.pid
                fin = time.monotonic() + 10
                while not os.path.exists(pret) and time.monotonic() < fin:
                    time.sleep(0.02)
                os.kill(os.getpid(), signal.SIGWINCH)  # le noyau vise l'attach
                self.assertEqual(proc.wait(timeout=10), 0)
                self.assertTrue(os.path.exists(recu))
            finally:
                relais.disarm()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            self.assertIs(signal.getsignal(signal.SIGWINCH), avant)

    def test_outside_main_thread_nothing_is_armed(self):
        avant = signal.getsignal(signal.SIGWINCH)
        relais = _WinchRelay()
        fil = threading.Thread(target=relais.arm)
        fil.start()
        fil.join()
        relais.disarm()
        self.assertIs(signal.getsignal(signal.SIGWINCH), avant)


class ConfigEnvTest(unittest.TestCase):
    def test_loaded_file_replaces_a_stale_inherited_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{}")
            env = {"AMEESH_CONFIG": "/nulle/part/config-ancienne-base.json",
                   "AGENT_MESH_CONFIG": "/nulle/part/autre.json"}
            self.assertEqual(_attach_config_env(Config(config_file=path), env), "")
            self.assertEqual(env["AMEESH_CONFIG"], path)
            self.assertEqual(env["AGENT_MESH_CONFIG"], path)

    def test_vanished_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            env: dict = {}
            refus = _attach_config_env(Config(config_file=path), env)
        self.assertIn(path, refus)
        self.assertIn("not launched", refus)
        self.assertNotIn("AMEESH_CONFIG", env)

    def test_defaults_drop_any_inherited_path(self):
        env = {"AMEESH_CONFIG": "/nulle/part.json", "AGENT_MESH_CONFIG": "/x.json"}
        self.assertEqual(_attach_config_env(Config(), env), "")
        self.assertNotIn("AMEESH_CONFIG", env)
        self.assertNotIn("AGENT_MESH_CONFIG", env)


class AttachLocalTest(PgTestCase):
    """De bout en bout : `run_attach` lance le faux harnais."""

    def setUp(self):
        super().setUp()
        self.register("att8123", "claude", cwd=self.tmp)
        patch = mock.patch.dict(os.environ, {
            "AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
            # la valeur périmée du constat : héritée par l'attach, jamais lue
            "AMEESH_CONFIG": "/nulle/part/config-ancienne-base.json"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_resize_and_config_reach_the_harness(self):
        vu = os.path.join(self.tmp, "config-vue")
        recu = os.path.join(self.tmp, "winch-recu")
        pret = os.path.join(self.tmp, "harnais-pret")
        commande = (
            "printf %%s \"$AMEESH_CONFIG\" > %(vu)s; "
            "trap 'touch %(recu)s' WINCH; touch %(pret)s; i=0; "
            "while [ ! -e %(recu)s ] && [ $i -lt 200 ]; do sleep 0.05; i=$((i+1)); done"
            % {"vu": shlex.quote(vu), "recu": shlex.quote(recu),
               "pret": shlex.quote(pret)})

        def redimensionner() -> None:
            fin = time.monotonic() + 10
            while not os.path.exists(pret) and time.monotonic() < fin:
                time.sleep(0.02)
            os.kill(os.getpid(), signal.SIGWINCH)

        fil = threading.Thread(target=redimensionner, daemon=True)
        fil.start()
        with mock.patch.dict(os.environ, {"AMEESH_TEST_RUN": commande}):
            code = run_attach(self.cfg, self.db, "att8123", ttl=30, max_seconds=20)
        fil.join(timeout=5)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(recu), "SIGWINCH not relayed to the harness")
        with open(vu, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), os.path.abspath(self.cfg.config_file))

    def test_vanished_config_refuses_before_launch(self):
        cfg = dataclasses.replace(
            self.cfg, config_file=os.path.join(self.tmp, "disparu.json"))
        pret = os.path.join(self.tmp, "lance")
        with mock.patch.dict(os.environ, {"AMEESH_TEST_RUN": "touch %s" % shlex.quote(pret)}):
            code = run_attach(cfg, self.db, "att8123", ttl=30, max_seconds=5)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(pret))


if __name__ == "__main__":
    unittest.main()
