# SPDX-License-Identifier: AGPL-3.0-only
"""#7789 : pendant une session `attach`, rien ne s'écrit sur le terminal du harnais.

Constat manaty/ameesh#6001 : les lignes de journal du battement, du veilleur
et de l'attente d'inputs (`log_async` → `_AsyncLog` → stdout) tombaient dans
la TUI de Claude Code et la brouillaient. Le test attache un faux harnais sur
un vrai pty : il lit son terminal et y écrit son flux ; le banc vérifie
qu'aucune ligne de l'attach n'y apparaît. Le journal de fichier et la ligne
d'attente rendue après la session sont vérifiés de la même manière.
"""
from __future__ import annotations

import os
import pty
import shlex
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from ameesh import attach, mail
from ameesh.runner import _AsyncLog, _HarnessTerminal, run_attach

from .support import FAKEBIN, PgTestCase


class TerminalGuardTest(unittest.TestCase):
    """Le garde seul : les deux flux quittent le terminal puis y reviennent."""

    @staticmethod
    def _read(fd: int) -> str:
        os.set_blocking(fd, False)
        try:
            return os.read(fd, 65536).decode("utf-8", "replace")
        except BlockingIOError:
            return ""
        except OSError:
            return ""

    def test_stdout_and_stderr_leave_the_terminal_then_return(self):
        with tempfile.TemporaryDirectory() as directory:
            master, slave = pty.openpty()
            saved = {1: os.dup(1), 2: os.dup(2)}
            os.dup2(slave, 1)
            os.dup2(slave, 2)
            os.close(slave)
            guard = _HarnessTerminal(directory)
            try:
                guard.enter()
                print("pendant", flush=True)
                print("erreur", file=sys.stderr, flush=True)
                guard.restore()
                self.assertTrue(guard.wrote)
                with open(os.path.join(directory, "attach.log"),
                          encoding="utf-8") as fh:
                    contenu = fh.read()
                self.assertIn("pendant", contenu)
                self.assertIn("erreur", contenu)
                print("apres", flush=True)
                sortie = self._read(master)
                self.assertIn("apres", sortie)
                self.assertNotIn("pendant", sortie)
                self.assertNotIn("erreur", sortie)
            finally:
                guard.restore()
                for which, fd in saved.items():
                    os.dup2(fd, which)
                    os.close(fd)
                os.close(master)


class JournalFlushTest(unittest.TestCase):
    """#7789 : la session vide ses dernières lignes dans le fichier, borné."""

    def test_flush_drains_accepted_messages(self):
        recus: list[str] = []
        with mock.patch("ameesh.runner.log", side_effect=recus.append):
            journal = _AsyncLog()
            try:
                journal.write("m")
                self.assertTrue(journal.flush(timeout=5))
                self.assertEqual(recus, ["m"])
            finally:
                journal.close(timeout=5)

    def test_flush_is_bounded_when_the_sink_blocks(self):
        libere = threading.Event()
        with mock.patch("ameesh.runner.log",
                        side_effect=lambda _message: libere.wait(10)):
            journal = _AsyncLog()
            try:
                journal.write("bloque")
                time.sleep(0.1)
                debut = time.monotonic()
                self.assertFalse(journal.flush(timeout=0.2))
                self.assertLess(time.monotonic() - debut, 2.0)
                libere.set()
                self.assertTrue(journal.flush(timeout=5))
            finally:
                libere.set()
                journal.close(timeout=5)


class AttachTerminalTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.register("att82", "claude", cwd=self.tmp)
        patch = mock.patch.dict(os.environ, {"AMEESH_BIN_DIR": FAKEBIN,
                                             "AMEESH_TEST_LOG": self.turns_log})
        patch.start()
        self.addCleanup(patch.stop)

    def test_no_attach_byte_reaches_the_harness_terminal(self):
        # Le harnais lit son terminal (stdin) et reste vivant après son flux :
        # la fenêtre couvre l'attente d'inputs, le veilleur et l'échéance du
        # bail, qui sont les trois sources du constat #6001.
        marqueur = os.path.join(self.tmp, "harnais-pret")
        env = {
            "AMEESH_TEST_RUN": "touch %s" % shlex.quote(marqueur),
            "AMEESH_TEST_READ_STDIN": "1",
            "AMEESH_TEST_HANG_AFTER_RESULT": "10",
        }
        mail.send(self.db, "source", "att82", "pending input")
        self.db.execute("UPDATE agent_mailbox SET created_at = now() - "
                        "interval '11 minutes' WHERE recipient = 'att82'")
        # L'alerte est armée quand le harnais a effectivement pris le terminal :
        # avant, l'attente est simplement trop récente pour son seuil.
        delay_avant, check_avant = attach.INPUT_DELAY, attach.INPUT_CHECK
        self.addCleanup(setattr, attach, "INPUT_DELAY", delay_avant)
        self.addCleanup(setattr, attach, "INPUT_CHECK", check_avant)
        attach.INPUT_DELAY = 10 ** 9
        attach.INPUT_CHECK = 0.02

        master, slave = pty.openpty()
        saved = {0: os.dup(0), 1: os.dup(1), 2: os.dup(2)}
        for which in (0, 1, 2):
            os.dup2(slave, which)
        os.close(slave)
        blocs: list[str] = []

        def lire() -> None:
            while True:
                try:
                    bloc = os.read(master, 65536)
                except OSError:
                    return
                if not bloc:
                    return
                blocs.append(bloc.decode("utf-8", "replace"))

        lecteur = threading.Thread(target=lire, daemon=True)
        lecteur.start()
        arret = threading.Event()

        def reveiller() -> None:
            fin = time.monotonic() + 10
            while time.monotonic() < fin and not arret.is_set() \
                    and not os.path.exists(marqueur):
                time.sleep(0.01)
            if arret.is_set() or not os.path.exists(marqueur):
                return
            try:
                # La ligne attend le harnais dans son terminal : l'attach ne
                # doit ni la consommer ni écrire autour.
                os.write(master, b"bonjour\n")
            except OSError:
                return
            attach.INPUT_DELAY = 0

        controleur = threading.Thread(target=reveiller, daemon=True)
        controleur.start()
        try:
            with mock.patch.dict(os.environ, env):
                code = run_attach(self.cfg, self.db, "att82", ttl=30,
                                  max_seconds=2.5)
        finally:
            arret.set()
            for which, fd in saved.items():
                os.dup2(fd, which)
                os.close(fd)
            controleur.join(timeout=5)
            lecteur.join(timeout=5)
            os.close(master)
        self.assertEqual(code, 124)
        texte = "".join(blocs).replace("\r\n", "\n")
        lignes = texte.splitlines()
        # Le faux harnais a bien lu SON terminal.
        self.assertIn('"subtype": "stdin"', texte)
        self.assertIn('"line": "bonjour"', texte)
        # Aucune ligne du journal d'attach n'a touché ce terminal.
        for marqueur_interdit in ("[att82] attach:", "[att82] attach ends in",
                                  "[att82] harness stopped"):
            self.assertNotIn(marqueur_interdit, texte)
        # Le journal de fichier, lui, porte l'attente, le veilleur et la coupe.
        journal_path = os.path.join(self.cfg.agent_dir("att82"), "attach.log")
        with open(journal_path, encoding="utf-8") as fh:
            journal = fh.read()
        self.assertIn("[att82] attach: inputs waiting", journal)
        self.assertIn("[att82] attach ends in", journal)
        self.assertIn("[att82] attach: maximum duration reached", journal)
        self.assertIn("[att82] harness stopped", journal)
        # L'attente d'inputs est rendue après la session, sur le terminal.
        self.assertIn("attach: inputs were waiting", texte)
        debuts = [i for i, ligne in enumerate(lignes)
                  if '"subtype": "stdin"' in ligne]
        fins = [i for i, ligne in enumerate(lignes)
                if ligne.startswith("attach: ")]
        self.assertTrue(debuts, "le flux du harnais n'a pas été capturé")
        self.assertTrue(fins, "la ligne d'état de fin de session manque")
        debut, fin = debuts[0], fins[0]
        self.assertLess(debut, fin)
        parasites = [ligne for ligne in lignes[debut:fin]
                     if ligne and not ligne.startswith("{")]
        self.assertEqual(parasites, [])
