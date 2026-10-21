# SPDX-License-Identifier: AGPL-3.0-only
"""#2932 : la rotation d'un agent Codex juge sur le contexte du dernier appel.

Constat du 2026-10-21 : 161 des 202 rotations des 24 h précédentes étaient
chez les agents Codex (cx2 : 43 rotations pour 109 tours), à des « contextes »
de 445 k à 13,4 M jetons, au-delà de toute fenêtre réelle. `codex exec --json`
ne publie que l'usage cumulé de fin de tour ; sans relevé d'appel dans le
flux, `context_rotation_due` retombait sur le grand livre, la somme des
relectures du tour, que #2336 a écartée comme mesure du plafond. Le contexte
du dernier appel se lit dans le journal de session du fil
(`info.last_token_usage` du dernier `token_count`). Postgres réel ; faux
harnais `codex` du banc ; journaux factices (jamais `~/.codex`).
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from ameesh import cost, registry, storage
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

FIL = "codex-thread-1"  # le fil du faux harnais


def _appel(entree: int) -> dict:
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": entree, "cached_input_tokens": entree},
        "last_token_usage": {"input_tokens": entree, "cached_input_tokens": entree - 1000,
                             "output_tokens": 50}}}}


def _journal(sessions: str, fil: str, appels: list, fin: list | None = None) -> str:
    """Un journal de session Codex : un tour, un `token_count` par appel."""
    dossier = os.path.join(sessions, "2026", "10", "21")
    os.makedirs(dossier, exist_ok=True)
    path = os.path.join(dossier, "rollout-2026-10-21T01-00-00-%s.jsonl" % fil)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        for entree in appels:
            fh.write(json.dumps(_appel(entree)) + "\n")
        for event in fin or []:
            fh.write(json.dumps(event) + "\n")
    return path


class JournalDeSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-2932-")
        self.sessions = os.path.join(self.tmp, "sessions")
        self.book = cost.CostBook(state_dir=self.tmp, codex_sessions=self.sessions)

    def test_dernier_appel_pas_le_cumul(self):
        _journal(self.sessions, FIL, [60_000] * 9 + [61_000], fin=[
            # relevé de jauge seul (sans `info`), puis une ligne illisible
            {"type": "event_msg", "payload": {"type": "token_count", "info": None,
                                              "rate_limits": {}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": [1]}},
        ])
        usage = self.book.codex_last_call_usage(FIL)
        self.assertEqual(usage["input_tokens"], 61_000)

    def test_journal_absent_ou_muet(self):
        self.assertIsNone(self.book.codex_last_call_usage(FIL))
        self.assertIsNone(self.book.codex_last_call_usage(None))
        _journal(self.sessions, FIL, [])
        self.assertIsNone(self.book.codex_last_call_usage(FIL))


class RotationCodexTest(PgTestCase):
    PLAFOND = 400_000

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending CASCADE")
        self.sessions = os.path.join(self.tmp, "codex-sessions")

    def _worker(self, name: str):
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "codex", cwd=cwd)
        storage.of(self.db).operations.set_settings(
            name, {"context_max_tokens": str(self.PLAFOND)})
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _tour(self, worker) -> bool:
        # dix appels de 60 k : 600 k relus au tour, cumul de fin de tour
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": self.turns_log,
               "AMEESH_CODEX_SESSIONS": self.sessions,
               "AMEESH_TEST_CODEX_USAGE": json.dumps({
                   "input_tokens": 600_000, "cached_input_tokens": 590_000,
                   "output_tokens": 500})}
        with mock.patch.dict(os.environ, env, clear=False):
            return worker.run_turn({"kind": "prompt", "prompt": "travail", "ids": []})

    def test_la_somme_des_appels_ne_tourne_pas(self):
        """Dix appels à 60 k (somme 600 k, plafond 400 k) : pas de rotation."""
        _journal(self.sessions, FIL, [60_000] * 10)
        worker = self._worker("cx-somme")
        self.assertTrue(self._tour(worker))
        self.assertEqual(worker.current_session(), FIL)
        self.assertGreaterEqual(worker.last_turn_reread, 600_000)  # le grand livre
        self.assertEqual(worker.last_turn_context_reread(), (60_000, FIL))
        self.assertFalse(worker.context_rotation_due())

    def test_un_dernier_appel_au_dela_du_plafond_tourne(self):
        _journal(self.sessions, FIL, [60_000] * 9 + [450_000])
        worker = self._worker("cx-gros")
        self.assertTrue(self._tour(worker))
        self.assertEqual(worker.last_turn_context_reread(), (450_000, FIL))
        self.assertTrue(worker.context_rotation_due())

    def test_journal_illisible_repli_sur_le_grand_livre(self):
        """Sans journal, la mesure reste celle d'avant (grand livre)."""
        worker = self._worker("cx-sans")
        self.assertTrue(self._tour(worker))
        self.assertIsNone(worker.last_turn_context)
        self.assertEqual(worker.last_turn_context_reread(), (600_000, FIL))


if __name__ == "__main__":
    unittest.main()
