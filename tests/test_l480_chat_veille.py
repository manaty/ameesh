# SPDX-License-Identifier: AGPL-3.0-only
"""L480 (#480) : la veille du chat — décisions et courrier servis entre deux
messages du propriétaire.

Aucune horloge murale n'est la vérité : `observe`, `due` et `note_human`
reçoivent leur `now` (monotone injecté) et l'âge des décisions son `wall`. Le
proxy du terminal est exercé sur un vrai pty et des tubes, sans TTY : c'est le
relais (frappes → harnais, sortie → écran, ligne de veille → harnais) qui est
testé.
"""
from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from ameesh import chat, decisions, mail, registry, work

from .support import PgTestCase

HUMAN = "human:alice"


def _decision(i, *, urgent=False, overdue=False, due_ts=None, created=0.0):
    return {"id": i, "urgent": urgent, "overdue": overdue, "due_ts": due_ts,
            "created_ts": created, "age_s": 0}


def _mail(i, *, urgent=False, created=0.0):
    return {"id": i, "sender": "agent", "created_ts": created,
            "payload": {"urgent": True} if urgent else {}}


class _Clock:
    def __init__(self, start=1000.0):
        self.t = float(start)

    def __call__(self):
        return self.t


class VeilleLineTest(unittest.TestCase):
    def test_rien_de_nouveau_ne_sert_rien(self):
        self.assertIsNone(chat.veille_line([], set(), [], set(), human="h", now=1000.0))
        self.assertIsNone(chat.veille_line([_decision(1)], {1}, [_mail(9)], {9},
                                           human="h", now=1000.0))

    def test_decisions_et_courrier_en_une_ligne(self):
        now = 10_000.0
        line = chat.veille_line(
            [_decision(1, created=now - 7200), _decision(2, urgent=True)],
            set(), [_mail(9, urgent=True, created=now - 30)], set(),
            human="human:h", now=now)
        self.assertTrue(line.startswith("[veille ameesh]"))
        self.assertIn("2 nouvelles décisions", line)
        self.assertIn("dont 1 urgente ou à échéance proche", line)
        self.assertIn("la plus ancienne il y a 2h", line)
        self.assertIn("1 nouveau courrier (dont 1 urgent)", line)
        self.assertIn("présente-les à human:h", line)
        self.assertIn("ameesh decisions", line)
        # elle demande de présenter : jamais de réponse
        self.assertNotIn("ameesh decide ", line)

    def test_echeance_proche_et_retard_sont_pressants(self):
        now = 10_000.0
        due = chat.veille_line([_decision(3, due_ts=now + 600)], set(), [], set(),
                               human="h", now=now)
        self.assertIn("dont 1 urgente ou à échéance proche", due)
        late = chat.veille_line([_decision(4, overdue=True)], set(), [], set(),
                                human="h", now=now)
        self.assertIn("dont 1 urgente ou à échéance proche", late)

    def test_singulier_et_pluriel(self):
        now = 10_000.0
        one = chat.veille_line([_decision(5, created=now)], set(), [], set(),
                               human="h", now=now)
        self.assertIn("1 nouvelle décision", one)
        self.assertNotIn("la plus ancienne", one)      # moins d'une minute
        two = chat.veille_line([_decision(5, created=now), _decision(6, created=now)],
                               set(), [], set(), human="h", now=now)
        self.assertIn("2 nouvelles décisions", two)
        one_mail = chat.veille_line([], set(), [_mail(7)], set(), human="h", now=now)
        self.assertIn("1 nouveau courrier", one_mail)
        self.assertNotIn("urgent", one_mail)

    def test_seul_le_nouveau_compte(self):
        now = 10_000.0
        line = chat.veille_line([_decision(1), _decision(2)], {1}, [], set(),
                                human="h", now=now)
        self.assertIn("1 nouvelle décision", line)


class VeilleSchedulingTest(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.veille = chat.Veille(object(), "human:h", "chat-h", now=self.clock,
                                  log=lambda text: None)

    def observe(self, pending=(), inbox=()):
        return self.veille.observe(list(pending), list(inbox))

    def test_la_premiere_lecture_est_un_repere(self):
        self.clock.t = 1000.0
        self.assertIsNone(self.observe([_decision(1)], [_mail(2)]))
        self.assertIsNone(self.veille.due())          # rien de nouveau
        self.assertIsNone(self.observe([_decision(1)], [_mail(2)]))

    def test_une_nouvelle_decision_est_servie_apres_la_frappe(self):
        self.observe()
        self.clock.t = 1000.0
        self.assertIn("1 nouvelle décision", self.observe([_decision(1)]))
        self.veille.note_human(self.clock.t)
        self.assertIsNone(self.veille.due())                  # $human écrit
        self.clock.t += chat.VEILLE_IDLE_S + 0.1
        line = self.veille.due()
        self.assertIn("1 nouvelle décision", line)
        self.assertIsNone(self.veille.due())                  # servie une fois

    def test_deux_services_ne_se_suivent_pas_de_trop_pres(self):
        self.observe()
        self.clock.t = 1000.0
        self.observe([_decision(1)])
        self.clock.t += chat.VEILLE_IDLE_S + 0.1
        self.assertIsNotNone(self.veille.due())
        self.clock.t += 1.0
        self.observe([_decision(1), _decision(2)])
        self.clock.t += chat.VEILLE_IDLE_S + 0.1
        self.assertIsNone(self.veille.due())                  # trop tôt
        self.clock.t += chat.VEILLE_MIN_INTERVAL_S
        self.assertIn("1 nouvelle décision", self.veille.due())

    def test_une_session_occupee_ne_doit_pas_affamer_la_veille(self):
        self.observe()
        self.clock.t = 1000.0
        self.observe([_decision(1)])
        first_news_at = self.clock.t
        self.clock.t += 1.0
        self.observe([_decision(1), _decision(2)])
        news_at = self.clock.t
        self.assertLess(news_at, first_news_at + chat.VEILLE_MIN_INTERVAL_S)
        # $human tape sans arrêt : la ligne attend, puis passe au-delà du délai
        self.clock.t = news_at + chat.VEILLE_MAX_DEFER_S - 1.0
        self.veille.note_human(self.clock.t)
        self.assertIsNone(self.veille.due())
        self.clock.t = news_at + chat.VEILLE_MAX_DEFER_S + 0.1
        self.veille.note_human(self.clock.t)
        self.assertIn("1 nouvelle décision", self.veille.due())

    def test_les_repères_suivent_les_lectures(self):
        self.observe()
        self.clock.t = 1000.0
        self.observe([_decision(1)])
        self.clock.t += chat.VEILLE_IDLE_S + 0.1
        self.veille.due()
        # la décision 1 reste en attente (non répondue) : elle n'est plus neuve
        self.clock.t += 1.0
        self.assertIsNone(self.observe([_decision(1)]))
        self.assertIsNone(self.veille.due())


class VeilleArmingTest(unittest.TestCase):
    def _args(self, **kw):
        args = chat.build_parser().parse_args([])
        for key, value in kw.items():
            setattr(args, key, value)
        return args

    def test_arme_seulement_avec_un_terminal(self):
        args = self._args()
        self.assertIsNone(args.veille)
        with mock.patch.object(sys, "stdin", _Fake(True)), \
                mock.patch.object(sys, "stdout", _Fake(True)):
            self.assertTrue(chat.veille_armed(args))
        with mock.patch.object(sys, "stdin", _Fake(False)), \
                mock.patch.object(sys, "stdout", _Fake(True)):
            self.assertFalse(chat.veille_armed(args))

    def test_no_veille_et_popen_refusent(self):
        args = self._args(veille=False)
        with mock.patch.object(sys, "stdin", _Fake(True)), \
                mock.patch.object(sys, "stdout", _Fake(True)):
            self.assertFalse(chat.veille_armed(args))
            self.assertFalse(chat.veille_armed(self._args(), popen=object()))

    def test_delais_par_defaut(self):
        args = self._args()
        self.assertEqual(args.veille_every, chat.VEILLE_EVERY_S)
        self.assertLess(chat.VEILLE_IDLE_S, chat.VEILLE_MIN_INTERVAL_S)
        self.assertLess(chat.VEILLE_MIN_INTERVAL_S, chat.VEILLE_MAX_DEFER_S)


class _Fake:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


class _FakeVeille:
    """La veille vue par le proxy : une ligne prête, puis plus rien."""

    def __init__(self):
        self.text = None
        self.notes = []

    def note_human(self, now=None):
        self.notes.append(now)

    def due(self, now=None):
        text, self.text = self.text, None
        return text


class ServePtyTest(unittest.TestCase):
    """Le relais du terminal : frappes, sortie et ligne de veille, sur un pty."""

    def _read_until(self, fd, needle, timeout=10.0):
        data = b""
        deadline = time.monotonic() + timeout
        while needle not in data:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([fd], [], [], min(0.2, left))
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        return data

    def test_relais_des_frappes_de_la_sortie_et_de_la_veille(self):
        master, slave = pty.openpty()
        script = ("import sys\n"
                  "for line in sys.stdin:\n"
                  "    print('ECHO:' + line.strip(), flush=True)\n"
                  "    if line.strip() == 'quit':\n"
                  "        break\n")
        proc = subprocess.Popen([sys.executable, "-c", script], stdin=slave,
                                stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        veille = _FakeVeille()
        errors: list = []

        def relais():
            try:
                chat.serve_pty(proc, master, veille=veille, stdin_fd=in_r,
                               stdout_fd=out_w, poll=0.02)
            except Exception as exc:  # noqa: BLE001 — remonté au test
                errors.append(exc)
        thread = threading.Thread(target=relais, daemon=True)
        thread.start()
        try:
            os.write(in_w, b"bonjour\n")
            self.assertIn(b"ECHO:bonjour", self._read_until(out_r, b"ECHO:bonjour"))
            self.assertTrue(veille.notes)                 # la frappe est notée
            veille.text = "[veille ameesh] 1 nouvelle décision"
            seen = self._read_until(out_r, b"ECHO:[veille ameesh]")
            self.assertIn(b"ECHO:[veille ameesh] 1 nouvelle d", seen)
            os.write(in_w, b"quit\n")
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for fd in (master, in_r, in_w, out_r, out_w):
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_la_sortie_du_harnais_n_est_pas_perdue(self):
        master, slave = pty.openpty()
        proc = subprocess.Popen([sys.executable, "-c",
                                 "print('dernier mot', flush=True)"],
                                stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True)
        os.close(slave)
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        try:
            chat.serve_pty(proc, master, veille=None, stdin_fd=in_r,
                           stdout_fd=out_w, poll=0.02)
            self.assertIn(b"dernier mot", self._read_until(out_r, b"dernier mot"))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for fd in (master, in_r, in_w, out_r, out_w):
                try:
                    os.close(fd)
                except OSError:
                    pass


class VeilleDbTest(PgTestCase):
    """Les lignes réelles (décision, courrier) alimentent la veille : le
    contrat de champs est celui de `decisions.pending` et `mail.unread_active`."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "orch", harness="claude", host=self.cfg.host)
        self.db.execute("UPDATE agent_registry SET responsible = %s WHERE name = 'orch'",
                        (HUMAN,))
        lot = work.add(self.db, title="L480 : veille du chat", assignee="orch")
        work.move(self.db, lot["id"], "build")
        self.lot = lot

    def _veille(self):
        return chat.Veille(self.cfg, HUMAN, "chat-alice", log=lambda text: None)

    def test_une_decision_reelle_devient_une_ligne(self):
        decisions.ask(self.cfg, self.db, requester="orch", question="On fusionne ?",
                      options=["a=oui, fusionner", "b=non, attendre"],
                      lot=str(self.lot["id"]))
        pending = decisions.pending(self.db, human=HUMAN)
        self.assertTrue(pending)
        veille = self._veille()
        self.assertIsNone(veille.observe([], []))            # repère
        line = veille.observe(pending, [])
        self.assertIn("[veille ameesh]", line)
        self.assertIn("1 nouvelle décision", line)
        self.assertIn("ameesh decisions", line)

    def test_le_courrier_reel_devient_une_ligne(self):
        mail.send(self.db, "orch", "chat-alice", "le lot 12 est prêt")
        inbox = mail.unread_active(self.db, "chat-alice")
        self.assertTrue(inbox)
        veille = self._veille()
        veille.observe([], [])
        line = veille.observe([], inbox)
        self.assertIn("1 nouveau courrier", line)


if __name__ == "__main__":
    unittest.main()
