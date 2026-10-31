# SPDX-License-Identifier: AGPL-3.0-only
"""#5148 (CTX-02c) — le ``last_token_usage`` Codex alimente ``C_last``.

Constat : sur le corpus CTX-05a, les 335 tours Codex avaient ``C_last`` nul.
``codex exec --json`` ne publie qu'un usage cumulé de fin de tour ; le journal
de session du fil porte, lui, un ``last_token_usage`` par appel. Le
normalisateur CTX-02a ne recevait que le flux : la mesure existait dans le
journal (le legacy la lisait pour 251 tours) mais n'entrait jamais dans
``C_last``/``O_last``. Les 84 tours sans contexte legacy étaient les tours de
résumé de rotation (``plafonne=False``).

Ces tests couvrent :
* la lecture du journal **bornée au tour** (``task_started``) : un tour
  précédent ne prête jamais son usage ;
* les motifs d'absence distincts (``journal_missing``, ``no_call``,
  ``turn_not_in_journal``, ``last_token_usage_missing``) ;
* ``C_last``/``O_last`` alimentés au journal sans doubler ``R_turn`` ;
* un tour Codex réel du banc, y compris un tour de résumé, qui publie
  ``turn.context`` avec ``C_last`` mesuré (source ``journal``).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from unittest import mock

from ameesh import adapters, context_metrics, cost, registry
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase

FIL = "codex-thread-1"
CALL = {"input_tokens": 61_000, "cached_input_tokens": 60_000,
        "output_tokens": 500}


def _event(payload: dict) -> dict:
    return {"type": "event_msg", "payload": payload}


def _task(started_at: float | None) -> dict:
    payload = {"type": "task_started"}
    if started_at is not None:
        payload["started_at"] = started_at
    return _event(payload)


def _token(usage: dict | None) -> dict:
    info = {} if usage is None else {"last_token_usage": usage}
    return _event({"type": "token_count", "info": info})


def _journal(sessions: str, events: list) -> str:
    dossier = os.path.join(sessions, "2026", "10", "31")
    os.makedirs(dossier, exist_ok=True)
    path = os.path.join(dossier, "rollout-2026-10-31T01-00-00-%s.jsonl" % FIL)
    with open(path, "w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
    return path


class JournalTourTest(unittest.TestCase):
    """Le journal est lu depuis la fin, borné au ``task_started`` du tour."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ameesh-5148-")
        self.sessions = os.path.join(self.tmp, "sessions")
        self.book = cost.CostBook(state_dir=self.tmp,
                                  codex_sessions=self.sessions)

    def test_dernier_appel_du_tour_dans_la_fenetre(self):
        _journal(self.sessions, [
            _task(1000), _token({"input_tokens": 50_000, "output_tokens": 100}),
            _task(2000),
            _token({"input_tokens": 60_000, "output_tokens": 400}),
            _token({"input_tokens": 61_000, "output_tokens": 500}),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["reason"])
        self.assertEqual(found["usage"]["input_tokens"], 61_000)
        self.assertEqual(found["turn_started"], 2000.0)

    def test_tour_precedent_ne_prete_pas_son_usage(self):
        # Le dernier tour du journal n'est pas celui de la fenêtre : aucun
        # usage ne doit être attribué au tour observé.
        _journal(self.sessions, [
            _task(1000), _token({"input_tokens": 50_000, "output_tokens": 100}),
            _task(2000), _token({"input_tokens": 60_000, "output_tokens": 400}),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1500, 1600)
        self.assertIsNone(found["usage"])
        self.assertEqual(found["reason"],
                         context_metrics.REASON_TURN_NOT_IN_JOURNAL)

    def test_tour_commence_sans_appel(self):
        _journal(self.sessions, [_task(2000)])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["usage"])
        self.assertEqual(found["reason"], context_metrics.REASON_NO_CALL)

    def test_token_count_sans_last_usage(self):
        _journal(self.sessions, [
            _task(2000), _token(None),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["usage"])
        self.assertEqual(found["reason"],
                         context_metrics.REASON_LAST_USAGE_MISSING)

    def test_journal_absent(self):
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["usage"])
        self.assertEqual(found["reason"],
                         context_metrics.REASON_JOURNAL_MISSING)

    def test_journal_sans_marqueur_de_tour(self):
        # Rien ne prouve que l'usage est celui du tour : il n'est pas repris.
        _journal(self.sessions, [
            _token({"input_tokens": 60_000, "output_tokens": 400}),
        ])
        found = self.book.codex_turn_last_call_usage(FIL, 1995, 2010)
        self.assertIsNone(found["usage"])
        self.assertEqual(found["reason"],
                         context_metrics.REASON_TURN_NOT_IN_JOURNAL)


class NormalizerJournalTest(unittest.TestCase):
    def test_journal_alimente_c_last_sans_doubler_r_turn(self):
        normalizer = context_metrics.TurnNormalizer("codex", session="s")
        normalizer.total({"input_tokens": 600_000,
                          "cached_input_tokens": 590_000,
                          "output_tokens": 900})
        normalizer.journal_call(CALL)
        document = normalizer.finish()
        measures = document["measures"]
        self.assertEqual(measures["C_last"]["value"], 61_000)
        self.assertEqual(measures["C_last"]["source"], "journal")
        self.assertEqual(measures["C_last"]["quality"], "measured")
        self.assertEqual(measures["O_last"]["value"], 500)
        # Le journal ne publie qu'un appel : R_turn reste au cumul du flux,
        # ici inconnu faute de base, jamais la somme d'un seul appel.
        self.assertIsNone(measures["R_turn"]["value"])
        self.assertEqual(measures["R_turn"]["source"], "total")
        self.assertEqual(document["counts"]["journal_calls"], 1)

    def test_journal_muet_qualifie_l_absence(self):
        normalizer = context_metrics.TurnNormalizer("codex", session="s")
        normalizer.journal_unavailable(
            context_metrics.REASON_JOURNAL_MISSING)
        measures = normalizer.finish()["measures"]
        self.assertIsNone(measures["C_last"]["value"])
        self.assertEqual(measures["C_last"]["quality"], "unknown")
        self.assertIn("context_unknown", measures["C_last"]["reasons"])
        self.assertIn("journal_missing", measures["C_last"]["reasons"])
        self.assertIsNone(measures["O_last"]["value"])
        self.assertIn("output_unknown", measures["O_last"]["reasons"])

    def test_appel_de_flux_prioritaire_sur_le_journal(self):
        normalizer = context_metrics.TurnNormalizer("codex", session="s")
        normalizer.call({"input_tokens": 42_000, "output_tokens": 7}, "c1")
        normalizer.journal_call(CALL)
        measures = normalizer.finish()["measures"]
        self.assertEqual(measures["C_last"]["value"], 42_000)
        self.assertEqual(measures["C_last"]["source"], "stream")
        self.assertEqual(measures["R_turn"]["value"], 42_000)


class TurnContextCodexTest(PgTestCase):
    """Un tour Codex du banc publie ``C_last`` mesuré depuis son journal."""

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending CASCADE")
        self.sessions = os.path.join(self.tmp, "codex-sessions")

    def _worker(self, name: str):
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", name)
        os.makedirs(cwd, exist_ok=True)
        self.register(name, "codex", cwd=cwd)
        lease = registry.claim(self.db, name, runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, name), lease)
        self.addCleanup(worker.watchdog_stop.set)
        return worker

    def _tour(self, worker, prompt: str = "travail") -> bool:
        env = {
            "AMEESH_BIN_DIR": FAKEBIN,
            "AMEESH_TEST_LOG": self.turns_log,
            "AMEESH_CODEX_SESSIONS": self.sessions,
            "AMEESH_TEST_CODEX_USAGE": json.dumps({
                "input_tokens": 600_000, "cached_input_tokens": 590_000,
                "output_tokens": 900}),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            return worker.run_turn({"kind": "prompt", "prompt": prompt,
                                    "ids": []})

    def _published(self, agent: str) -> dict:
        rows = self.db.query(
            "select data from ameesh_events where type = %s"
            " and data->>'agent' = %s order by id desc limit 1",
            (cost.CONTEXT_EVENT, agent))
        self.assertTrue(rows, "aucun événement turn.context publié")
        return rows[0]["data"]

    def test_c_last_du_journal_dans_turn_context(self):
        _journal(self.sessions, [
            _task(time.time() - 1.0), _token(CALL),
        ])
        worker = self._worker("cx-journal")
        self.assertTrue(self._tour(worker))
        data = self._published("cx-journal")
        measures = data["context"]["measures"]
        self.assertEqual(measures["C_last"]["value"], 61_000)
        self.assertEqual(measures["C_last"]["quality"], "measured")
        self.assertEqual(measures["C_last"]["source"], "journal")
        self.assertEqual(measures["O_last"]["value"], 500)
        # Le legacy lit le même journal et garde sa valeur : rien ne change
        # pour la rotation.
        self.assertEqual(data["legacy"]["context_tokens"], 61_000)

    def test_tour_de_resume_publie_c_last_sans_context_legacy(self):
        # `plafonne=False` : le legacy n'appelle pas le journal, mais
        # l'observation le lit et publie C_last (les 84 tours du corpus).
        _journal(self.sessions, [
            _task(time.time() - 1.0), _token(CALL),
        ])
        worker = self._worker("cx-resume")
        self.assertTrue(self._tour(worker, prompt=adapters.SUMMARY_PROMPT))
        data = self._published("cx-resume")
        measures = data["context"]["measures"]
        self.assertEqual(measures["C_last"]["value"], 61_000)
        self.assertEqual(measures["C_last"]["source"], "journal")
        self.assertIsNone(data["legacy"]["context_tokens"])

    def test_journal_absent_reste_inconnu_avec_sa_cause(self):
        worker = self._worker("cx-sans-journal")
        self.assertTrue(self._tour(worker))
        data = self._published("cx-sans-journal")
        measures = data["context"]["measures"]
        self.assertIsNone(measures["C_last"]["value"])
        self.assertEqual(measures["C_last"]["quality"], "unknown")
        self.assertIn("context_unknown", measures["C_last"]["reasons"])
        self.assertIn("journal_missing", measures["C_last"]["reasons"])
        self.assertIsNone(data["legacy"]["context_tokens"])


if __name__ == "__main__":
    unittest.main()
