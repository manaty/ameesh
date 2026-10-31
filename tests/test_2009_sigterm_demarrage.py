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

#2032 (suite) : la fenêtre de quelques µs entre la livraison d'un signal et la
remise au défaut pouvait encore perdre l'arrêt si le gestionnaire Python
n'avait pas tourné avant d'être remplacé. Un tube d'éveil
(`signal.set_wakeup_fd`) reçoit l'octet du gestionnaire C à la livraison et
survit à la remise au défaut ; les diagnostics du chemin de remplacement
n'attendent plus le puits.
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
                mock.patch.object(runner_mod, "_harden_executor_environment",
                                  return_value=True), \
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


class CaptureArretPendantLeRemplacementTest(unittest.TestCase):
    """#2032 : la remise au défaut ne perd plus un arrêt livré dans la fenêtre.

    La course : le gestionnaire C de CPython note le signal, puis
    `signal.signal(sig, SIG_DFL)` remplace le gestionnaire Python avant qu'il
    n'ait tourné — le drapeau part avec lui. Le tube d'éveil reçoit l'octet du
    gestionnaire C lui-même, à la livraison, et survit à la remise au défaut.
    """

    def _capture(self):
        capture = runner_mod._CaptureArret()
        self.addCleanup(capture.fermer)
        return capture

    def _temoin(self, signum=signal.SIGTERM):
        """Gestionnaire muet, rendu à la fin du témoin."""
        def muet(_signum, _frame):
            pass

        ancien = signal.signal(signum, muet)
        self.addCleanup(signal.signal, signum, ancien)

    def test_octet_ecrit_a_la_livraison_avant_le_gestionnaire_python(self):
        capture = self._capture()
        vus = {}

        def temoin(_signum, _frame):
            vus["vu"] = capture.arret_vu()

        ancien = signal.signal(signal.SIGTERM, temoin)
        self.addCleanup(signal.signal, signal.SIGTERM, ancien)
        capture.armer()
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(vus.get("vu"),
                        "le tube d'éveil est écrit par le gestionnaire C, avant "
                        "que le gestionnaire Python ne tourne")

    def test_arret_livre_survit_a_la_remise_au_defaut(self):
        capture = self._capture()
        self._temoin()
        capture.armer()
        os.kill(os.getpid(), signal.SIGTERM)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)  # la remise au défaut
        self.assertTrue(capture.arret_vu(),
                        "l'arrêt livré ne doit pas partir avec le gestionnaire Python")

    def test_aucun_arret_sans_signal(self):
        capture = self._capture()
        capture.armer()
        self.assertFalse(capture.arret_vu())

    def test_signal_etranger_n_est_pas_un_arret(self):
        capture = self._capture()
        self._temoin(signal.SIGUSR1)
        capture.armer()
        os.kill(os.getpid(), signal.SIGUSR1)
        self.assertFalse(capture.arret_vu(), "seuls SIGTERM/SIGINT sont des arrêts")


class _CaptureDoublure:
    """Doublure du tube d'éveil : l'ordre et le verdict sont ce qu'on vérifie."""

    def __init__(self, *, vu: bool = False, journal: list | None = None) -> None:
        self.vu = vu
        self.journal = journal if journal is not None else []
        self.arme = False
        self.ferme = False

    def armer(self) -> None:
        self.arme = True
        self.journal.append("armer")

    def arret_vu(self) -> bool:
        return self.vu

    def fermer(self) -> None:
        self.ferme = True


class CaptureBranchement2032Test(unittest.TestCase):
    """#2032 : la capture est armée avant la remise au défaut et fait sortir en 0."""

    def test_capture_armee_avant_la_remise_au_defaut(self):
        journal: list = []
        capture = _CaptureDoublure(journal=journal)

        def faux_signal(signum, handler):
            journal.append(("signal", signum, handler))
            return signal.SIG_DFL

        with mock.patch.object(runner_mod, "_CaptureArret", return_value=capture), \
                mock.patch.object(runner_mod.signal, "signal",
                                  side_effect=faux_signal), \
                mock.patch.object(runner_mod, "_restart_with_installed_code",
                                  return_value=False), \
                mock.patch.object(runner_mod, "_message_remplacement"):
            self.assertEqual(runner_mod._replace_image_or_stop(_FakeRunner()),
                             runner_mod.EXIT_RESTART)
        self.assertEqual(journal[0], "armer",
                         "le tube doit être armé avant la remise au défaut")
        remises = [entree[1] for entree in journal[1:] if entree[0] == "signal"]
        self.assertEqual(remises[:len(runner_mod.STOP_SIGNALS)],
                         list(runner_mod.STOP_SIGNALS))
        self.assertEqual(len(remises), 2 * len(runner_mod.STOP_SIGNALS),
                         "les dispositions d'avant sont rendues dans le `finally`")
        self.assertTrue(capture.ferme, "le tube d'éveil est rendu dans tous les cas")

    def test_arret_capture_pendant_le_remplacement_fait_sortir_en_zero(self):
        capture = _CaptureDoublure(vu=True)
        with mock.patch.object(runner_mod, "_CaptureArret", return_value=capture), \
                mock.patch.object(runner_mod, "_restart_with_installed_code") as restart, \
                mock.patch.object(runner_mod, "_message_remplacement"):
            self.assertEqual(runner_mod._replace_image_or_stop(_FakeRunner()), 0)
        restart.assert_not_called()
        self.assertTrue(capture.arme)
        self.assertTrue(capture.ferme)


class RemplacementSansAttenteTest(unittest.TestCase):
    """#2032 : le diagnostic du chemin de remplacement n'attend jamais le puits."""

    @staticmethod
    def _tube_plein():
        """Remplit le tube jusqu'à EAGAIN, puis rend une écriture bloquante."""
        r, w = os.pipe()
        try:
            os.set_blocking(w, False)
            try:
                while True:
                    # Un octet : même le dernier espace libre doit être pris.
                    os.write(w, b"x")
            except BlockingIOError:
                pass
            finally:
                os.set_blocking(w, True)
        except BaseException:
            os.close(r)
            os.close(w)
            raise
        return r, w

    def test_ecriture_sans_attendre_tube_plein(self):
        r, w = self._tube_plein()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        debut = time.monotonic()
        runner_mod._ecrire_sans_attendre(w, b"diagnostic\n")
        self.assertLess(time.monotonic() - debut, 1.0,
                        "un tube plein ne doit pas retenir le diagnostic")

    def test_message_de_remplacement_sans_attente(self):
        with mock.patch.object(runner_mod, "_ecrire_sans_attendre") as ecrire:
            runner_mod._message_remplacement("agent-runner: essai")
        ecrire.assert_called_once_with(2, b"agent-runner: essai\n")

    def test_remplacement_utilise_l_ecriture_sans_attente(self):
        with mock.patch.object(runner_mod, "_ecrire_sans_attendre") as ecrire, \
                mock.patch.object(runner_mod, "_restart_with_installed_code") as restart:
            self.assertEqual(
                runner_mod._replace_image_or_stop(_FakeRunner(draining=True)), 0)
        restart.assert_not_called()
        self.assertEqual(ecrire.call_args[0][0], 2)

    def test_restart_utilise_l_ecriture_sans_attente(self):
        with mock.patch.object(runner_mod, "_restart_argv", return_value=[]), \
                mock.patch.object(runner_mod, "_ecrire_sans_attendre") as ecrire:
            self.assertFalse(runner_mod._restart_with_installed_code())
        self.assertEqual(ecrire.call_args[0][0], 2)


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
