# SPDX-License-Identifier: AGPL-3.0-only
"""#2009 : un SIGTERM reçu au démarrage de l'exécuteur n'est jamais avalé.

Incident du 16/10 (deepseek1, hôte agents-2) : `systemctl restart` a été
demandé pendant que l'exécuteur se remplaçait (L790, paquet 1.11.2 → 1.11.3).
Le SIGTERM est arrivé pendant l'attente de la base au démarrage, dans le
gestionnaire temporaire de `_connect_at_start` : la connexion a réussi, le
signal a été oublié — aucun log, et l'unité est restée
`deactivating/stop-sigterm` pendant que l'exécuteur reprenait des tours
(réveil courrier de 09:47Z) jusqu'à un second SIGTERM, 26 minutes plus tard.

Le correctif ferme les deux fenêtres où le signal se perdait :

* `_connect_at_start` lève `StopRequested` — l'appelant sort en 0, sans tour
  ni bail, même quand la base finit par répondre ;
* le remplacement d'image (L790) renonce à se remplacer si un arrêt a déjà été
  traité, et remet SIGTERM/SIGINT au défaut avant l'`exec` : un signal reçu
  pendant le remplacement tue le processus (l'arrêt est honoré) au lieu d'être
  consommé par le gestionnaire de l'image sortante.

Témoins : unités déterministes (le signal est posé par la doublure de
connexion, ou pendant le remplacement), bout en bout (connexion de démarrage
ralentie par `sitecustomize`, vrai SIGTERM), et le témoin attendu du lot — un
réveil courrier pendant le drainage ne lance aucun tour.
"""
from __future__ import annotations

import os
import signal
import threading
import time
import unittest
from unittest import mock

from ameesh import config as config_mod
from ameesh import mail, registry
from ameesh import runner as runner_mod

from .support import SRC, PgTestCase


class _FakeDb:
    """Base de doublure : retient sa fermeture."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class ConnectAtStartTest(unittest.TestCase):
    """`_connect_at_start` ne consomme plus un signal d'arrêt."""

    def test_signal_pendant_la_connexion_n_est_pas_avale(self):
        """Le cas de l'incident : la base répond, le signal ne doit pas mourir."""
        db = _FakeDb()

        def connect(_cfg):
            os.kill(os.getpid(), signal.SIGTERM)
            return db

        with mock.patch.object(runner_mod.db_mod, "connect", side_effect=connect), \
                mock.patch.object(runner_mod, "log"):
            with self.assertRaises(runner_mod.StopRequested) as ctx:
                runner_mod._connect_at_start(config_mod.Config(), wait=True)
        self.assertEqual(ctx.exception.signum, signal.SIGTERM)
        self.assertTrue(db.closed, "la base ouverte est rendue avant la sortie")

    def test_signal_pendant_l_attente_de_la_base(self):
        """Un signal pendant l'attente croissante sort proprement lui aussi."""
        def connect(_cfg):
            os.kill(os.getpid(), signal.SIGTERM)
            raise runner_mod.db_mod.Unavailable("connection refused")

        with mock.patch.object(runner_mod.db_mod, "connect", side_effect=connect), \
                mock.patch.object(runner_mod, "log"):
            with self.assertRaises(runner_mod.StopRequested):
                runner_mod._connect_at_start(config_mod.Config(), wait=True)

    def test_echec_immediat_en_once_inchange(self):
        """`--once` : une base injoignable reste une sortie en échec (L106)."""
        with mock.patch.object(runner_mod.db_mod, "connect",
                               side_effect=runner_mod.db_mod.Unavailable("refus")), \
                mock.patch.object(runner_mod, "log"):
            self.assertIsNone(runner_mod._connect_at_start(config_mod.Config(),
                                                           wait=False))


class MainSortieSansTourTest(unittest.TestCase):
    """`main` sort en 0, sans tour, quand le démarrage a reçu un arrêt."""

    def test_stoprequested_rend_zero(self):
        with mock.patch.object(runner_mod, "load_config",
                               return_value=config_mod.Config()), \
                mock.patch("ameesh.host_gate.gate_choice"), \
                mock.patch.object(runner_mod, "_connect_at_start",
                                  side_effect=runner_mod.StopRequested(signal.SIGTERM)):
            self.assertEqual(runner_mod.main(["--agents", "x"]), 0)


class _FakeRunner:
    """Doublure minimale : `stop_on_signal` comme le vrai exécuteur."""

    def __init__(self, *, draining: bool = False, sigterm: bool = False) -> None:
        self._draining, self._sigterm = draining, sigterm

    def stop_on_signal(self) -> bool:
        return self._draining or self._sigterm


class ReplaceImageOrStopTest(unittest.TestCase):
    """Le remplacement d'image (L790) ne se fait pas quand un arrêt est demandé."""

    def test_drainage_en_cours_ne_se_remplace_pas(self):
        with mock.patch.object(runner_mod, "_restart_with_installed_code") as restart:
            self.assertEqual(
                runner_mod._replace_image_or_stop(_FakeRunner(draining=True)), 0)
        restart.assert_not_called()

    def test_retrait_medie_ne_se_remplace_pas(self):
        with mock.patch.object(runner_mod, "_restart_with_installed_code") as restart:
            self.assertEqual(
                runner_mod._replace_image_or_stop(_FakeRunner(sigterm=True)), 0)
        restart.assert_not_called()

    def test_signal_arrive_pendant_le_remplacement_ne_se_remplace_pas(self):
        """Le second contrôle, dispositions remises au défaut, rattrape la course."""
        class _Runner:
            def __init__(self) -> None:
                self.appels = 0

            def stop_on_signal(self) -> bool:
                self.appels += 1
                return self.appels >= 2

        with mock.patch.object(runner_mod, "_restart_with_installed_code") as restart:
            self.assertEqual(runner_mod._replace_image_or_stop(_Runner()), 0)
        restart.assert_not_called()

    def test_remplacement_normal_rejoue_l_invocation(self):
        with mock.patch.object(runner_mod, "_restart_with_installed_code",
                               return_value=False) as restart:
            self.assertEqual(runner_mod._replace_image_or_stop(_FakeRunner()),
                             runner_mod.EXIT_RESTART)
        restart.assert_called_once_with()

    def test_dispositions_au_defaut_pendant_l_exec_puis_rendues(self):
        """Un signal reçu pendant l'`exec` tue le processus : l'arrêt est honoré."""
        vus = {}

        def temoin(_signum, _frame):  # pragma: no cover - jamais appelé
            pass

        ancien = signal.signal(signal.SIGTERM, temoin)
        self.addCleanup(signal.signal, signal.SIGTERM, ancien)

        def restart():
            vus["term"] = signal.getsignal(signal.SIGTERM)
            vus["int"] = signal.getsignal(signal.SIGINT)
            return False

        with mock.patch.object(runner_mod, "_restart_with_installed_code",
                               side_effect=restart):
            self.assertEqual(runner_mod._replace_image_or_stop(_FakeRunner()),
                             runner_mod.EXIT_RESTART)
        self.assertIs(vus["term"], signal.SIG_DFL)
        self.assertIs(vus["int"], signal.SIG_DFL)
        self.assertIs(signal.getsignal(signal.SIGTERM), temoin,
                      "les dispositions d'avant sont rendues quand l'exec échoue")


class RunnerStopOnSignalTest(unittest.TestCase):
    """`stop_on_signal` ne confond pas un arrêt reçu avec `shutdown`."""

    @staticmethod
    def _runner(**kw):
        runner = runner_mod.Runner.__new__(runner_mod.Runner)
        runner.draining = threading.Event()
        runner.sigterm_requested = False
        for cle, valeur in kw.items():
            setattr(runner, cle, valeur)
        return runner

    def test_aucun_signal(self):
        self.assertFalse(self._runner().stop_on_signal())

    def test_drainage_classique(self):
        runner = self._runner()
        runner.draining.set()
        self.assertTrue(runner.stop_on_signal())

    def test_retrait_medie(self):
        self.assertTrue(self._runner(sigterm_requested=True).stop_on_signal())

    def test_signal_sans_drainage_apres_shutdown_ne_se_remplace_pas(self):
        for once, drain_seconds in ((False, 0), (True, 120)):
            with self.subTest(once=once, drain_seconds=drain_seconds):
                runner = self._runner(once=once, drain_seconds=drain_seconds,
                                      stop=threading.Event(), wake_all=threading.Event())
                # shutdown a déjà posé stop après le choix EXIT_RESTART.
                runner.stop.set()
                with mock.patch.object(runner_mod, "log_async"), \
                        mock.patch.object(runner_mod, "_restart_with_installed_code") as restart:
                    runner.request_stop(signal.SIGTERM)
                    self.assertEqual(runner_mod._replace_image_or_stop(runner), 0)
                restart.assert_not_called()

    def test_shutdown_sans_signal_ne_bloque_pas_le_remplacement(self):
        runner = self._runner(stop=threading.Event())
        runner.stop.set()
        self.assertFalse(runner.stop_on_signal())


#: #2009 : injecté par `PYTHONPATH` dans le processus de l'exécuteur. La
#: connexion de démarrage est ralentie UNE fois (marqueur), pour que le test y
#: place un vrai SIGTERM — la fenêtre exacte de l'incident.
SITECUSTOMIZE_L2009 = '''\
"""Témoin #2009 : ralentit la connexion de démarrage pour y placer un SIGTERM."""
import os
import time

_marqueur = os.environ.get("L2009_CONNECT_MARK")
if _marqueur and not os.path.exists(_marqueur):
    import ameesh.db as _db
    _vrai = _db.connect

    def _lent(cfg):
        try:
            with open(_marqueur, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
        except OSError:
            pass
        time.sleep(float(os.environ.get("L2009_CONNECT_PAUSE", "4")))
        return _vrai(cfg)

    _db.connect = _lent
'''


class SigtermAuDemarrageBoutEnBoutTest(PgTestCase):
    """Le SIGTERM de `systemctl restart` pendant la connexion arrête l'unité."""

    def test_sigterm_pendant_la_connexion_de_demarrage(self):
        nom = "attend"
        cwd = os.path.join(self.tmp, "work", nom)
        os.makedirs(cwd, exist_ok=True)
        self.register(nom, "claude", cwd=cwd, prompt="consigne du tour")
        inject = os.path.join(self.tmp, "inject2009")
        os.makedirs(inject, exist_ok=True)
        with open(os.path.join(inject, "sitecustomize.py"), "w", encoding="utf-8") as fh:
            fh.write(SITECUSTOMIZE_L2009)
        marqueur = os.path.join(self.tmp, "connect2009")
        env = self.env(AMEESH_DRAIN_SECONDS="120",
                       L2009_CONNECT_MARK=marqueur, L2009_CONNECT_PAUSE="4",
                       PYTHONPATH=inject + os.pathsep + SRC)
        proc = self.runner_popen("--agents", nom, "--poll", "1", env=env)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(10)))
        self.wait_for(lambda: os.path.exists(marqueur), timeout=30)
        proc.send_signal(signal.SIGTERM)
        sortie, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, sortie)
        self.assertIn("SIGTERM received at startup", sortie)
        self.assertEqual(self.turns(), [], "aucun tour ne part après le signal")
        self.assertIsNone(registry.get(self.db, nom)["lease_owner"])


class ReveilCourrierPendantLeDrainageTest(PgTestCase):
    """Témoin du lot : un réveil courrier pendant le drainage ne lance rien."""

    def _lance(self, nom: str, **env):
        cwd = os.path.join(self.tmp, "work", nom)
        os.makedirs(cwd, exist_ok=True)
        self.register(nom, "claude", cwd=cwd, prompt="consigne du tour")
        proc = self.runner_popen("--agents", nom, "--poll", "1", env=self.env(**env))
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait(10)))
        self.wait_for(lambda: len(self.turns()) == 1, timeout=30)  # le tour a démarré
        return proc

    def test_reveil_courrier_pendant_le_drainage_ne_lance_aucun_tour(self):
        proc = self._lance("drain-courrier", AMEESH_DRAIN_SECONDS="120",
                           AMEESH_TEST_SLEEP="6")
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.5)
        message_id = mail.send(self.db, "pair", "drain-courrier",
                               "message reçu pendant le drainage")
        sortie, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, sortie)
        self.assertIn("draining", sortie)
        self.assertEqual(len(self.turns()), 1, "aucun tour ne part après le SIGTERM")
        self.assertEqual([m["id"] for m in mail.unread(self.db, "drain-courrier")],
                         [message_id], "le message attend l'exécuteur suivant")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
