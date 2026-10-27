# SPDX-License-Identifier: AGPL-3.0-only
"""#2934 — une note de pause budget par nature, pas à chaque point de jauge."""
from __future__ import annotations

import dataclasses
import os
import unittest
from unittest import mock

from ameesh import accounts, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


def raison(pourcentage: int, *, compte: str = "tertiaire",
           fenetre: str = "seven_day", echeance: str = "14:20") -> str:
    """Une raison de pause budget réaliste, comme `accounts.choose` l'écrit."""
    return ("pause (account %s, %s %d %%) until %s; all claude accounts at "
            "threshold — primaire: seven_day 92 %% used (pace 55 %%); "
            "%s: %s %d %% used (pace 55 %%)" % (
                compte, fenetre, pourcentage, echeance, compte, fenetre, pourcentage))


class PauseNatureTest(unittest.TestCase):
    """La nature ne retient que le compte, la fenêtre et l'échéance."""

    def test_trois_pourcentages_meme_compte_meme_fenetre_une_seule_nature(self):
        natures = {accounts.pause_nature(raison(p)) for p in (81, 82, 83)}
        self.assertEqual(len(natures), 1, natures)

    def test_compte_fenetre_ou_echeance_change_la_nature(self):
        base = accounts.pause_nature(raison(83))
        for autre in (raison(83, compte="secondaire"),
                      raison(83, fenetre="five_hour"),
                      raison(83, echeance="14:23")):
            with self.subTest(autre=autre):
                self.assertNotEqual(base, accounts.pause_nature(autre))

    def test_compte_par_defaut_et_pourcentage_sans_espace(self):
        a = accounts.pause_nature(
            "pause (account (default), five_hour 96%) until 14:20; plan claude "
            "five_hour: 96% used with 65% of the window elapsed (pace cap 41%)")
        b = accounts.pause_nature(
            "pause (account (default), five_hour 97%) until 14:20; plan claude "
            "five_hour: 97% used with 66% of the window elapsed (pace cap 41%)")
        self.assertEqual(a, b)
        self.assertIn("(default)", a)

    def test_raison_sans_preambule_ignore_les_mesures_continues(self):
        a = accounts.pause_nature(
            "hourly budget (pay-per-token): 12.34 $ over 1 h (cap 10.00 $)")
        b = accounts.pause_nature(
            "hourly budget (pay-per-token): 13.00 $ over 1 h (cap 10.00 $)")
        self.assertEqual(a, b)
        self.assertNotEqual(a, accounts.pause_nature(
            "daily budget (pay-per-token): 13.00 $ over 24 h (cap 10.00 $)"))


class BudgetPauseNoteTest(PgTestCase):
    def _worker(self):
        cfg = dataclasses.replace(self.cfg, budget_usd_per_hour=10.0,
                                  budget_check_interval=0.0)
        runner = Runner(cfg, self.db, once=True)
        runner.budget_check_interval = 0.0  # chaque contrôle passe, sans attente
        cwd = os.path.join(self.tmp, "work", "notes")
        os.makedirs(cwd, exist_ok=True)
        self.register("notes", "claude", cwd=cwd)
        lease = registry.claim(self.db, "notes", runner.runner_id, 3600)
        return runner, AgentWorker(runner, registry.get(self.db, "notes"), lease)

    def _notes(self, worker, raisons):
        """Joue les contrôles, rend les notes de fil écrites."""
        notes = []
        with mock.patch.object(worker, "budget_reason", side_effect=raisons), \
                mock.patch.object(
                    worker, "_fil_note",
                    side_effect=lambda text, meta=None: notes.append((text, meta))):
            for _ in raisons:
                self.assertFalse(worker.budget_ok())
        return notes

    def test_trois_controles_81_82_83_une_seule_note(self):
        _runner, worker = self._worker()
        raisons = [raison(81), raison(82), raison(83)]
        try:
            notes = self._notes(worker, raisons)
        finally:
            worker.watchdog_stop.set()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("81 %", notes[0][0])
        # le pourcentage continue de vivre dans `status_text` et dans la raison
        self.assertIn("83 %", registry.get(self.db, "notes")["status_text"])
        self.assertEqual(worker._budget_reason, raison(83))

    def test_compte_ou_echeance_change_ecrit_une_nouvelle_note(self):
        _runner, worker = self._worker()
        raisons = [raison(83), raison(83, echeance="14:23"),
                   raison(83, compte="secondaire"), raison(83, compte="secondaire")]
        try:
            notes = self._notes(worker, raisons)
        finally:
            worker.watchdog_stop.set()
        self.assertEqual(len(notes), 3, notes)
        self.assertIn("secondaire", registry.get(self.db, "notes")["status_text"])

    def test_4020_fenetre_courte_sans_note_au_fil(self):
        """#4020 : l'écart de départ, le cooldown et le plafond de tours (#3779)
        restent dans `status_text` ; une pause de jauge qui suit a sa note."""
        _runner, worker = self._worker()
        courtes = [
            "account primaire: next turn in 25s (start gap 30s between turns, "
            "#3779; last start 16:45)",
            "account primaire: next turn in 26s (start gap 30s between turns, "
            "#3779; last start 16:46)",
            "account primaire: short-window cooldown until 16:52 (in 4 min; 3 "
            "provider 429 in 2 min, shared burst bucket, #3779)",
            "account primaire: 2/2 concurrent turns (provider burst bucket shared "
            "by sessions, #3779; running: cx1, cx2)",
        ]
        try:
            notes = self._notes(worker, courtes + [raison(83)])
        finally:
            worker.watchdog_stop.set()
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("83 %", notes[0][0])
        self.assertEqual(notes[0][1]["action"], "budget")
