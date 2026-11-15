# SPDX-License-Identifier: AGPL-3.0-only
"""#7494 — un tour Codex `exec` clos avant `turn.completed` est compté.

Constat : le tour 85007 de mty-amh-impl-11 (11:41:13Z → 12:11:28Z, 1803 s) a
été clos par le plafond de durée (`turn_max_seconds` = 1800) sans que le
harnais publie son `turn.completed`. `codex exec --json` ne portant son usage
que dans cet événement, `CostBook.turn_usage` rendait zéro et `record`
n'avançait aucun cumul : le grand livre perdait tout le tour.

Le journal de session du fil (`rollout-…-<fil>.jsonl`) porte, lui, un
`token_count` par appel avec le `last_token_usage` de l'appel et le
`total_token_usage` cumulé du fil. Ces tests vérifient que le grand livre y
lit, **borné à la fenêtre du tour** :

* l'usage du tour (delta du cumul du fil, ou somme des appels à défaut) ;
* le cumul de fin de tour, pour que le tour suivant ne l'absorbe pas ;
* l'absence de tout emprunt à un autre tour du fil (avant, après, réparation
  tardive) ;
* la priorité du `turn.completed` du flux quand il existe (aucun double compte).
"""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from ameesh import registry, runner as runner_mod, storage
from ameesh.runner import AgentWorker, Runner

from .support import FAKEBIN, PgTestCase
from .test_cost import Sandbox

THREAD = "01a1df02-f182-7fa3-ac6b-75b955293004"


def _event(payload: dict) -> dict:
    return {"type": "event_msg", "payload": payload}


def _task(started_at: float) -> dict:
    return _event({"type": "task_started", "started_at": started_at})


def _token(last: dict, total: dict | None = None) -> dict:
    info = {"last_token_usage": last}
    if total is not None:
        info["total_token_usage"] = total
    return _event({"type": "token_count", "info": info})


def _journal(sessions: str, events: list, thread: str = THREAD) -> str:
    dossier = os.path.join(sessions, "2026", "11", "15")
    os.makedirs(dossier, exist_ok=True)
    path = os.path.join(dossier, "rollout-2026-11-15T11-41-00-%s.jsonl" % thread)
    with open(path, "w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
    return path


def _tokens(input_tokens: int, cached: int, output: int) -> dict:
    return {"input_tokens": input_tokens, "cached_input_tokens": cached,
            "output_tokens": output}


class CodexJournalInterrompuTest(Sandbox, PgTestCase):
    """Le grand livre relit le journal quand le flux n'a pas de completion."""

    def setUp(self) -> None:
        super().setUp()
        self.agent("a1", "codex", "gpt-6.1-sol")

    def _resource(self, turn_id: str, ended: float) -> None:
        self.db.execute(
            "INSERT INTO turn_resources"
            " (turn_id, agent, host, pgid, started_at, ended_at, status)"
            " VALUES (%s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s), 'done')",
            (turn_id, "a1", "ameesh-agents-1", 4242, 1990, ended))

    def _row(self) -> dict:
        rows = self.db.query(
            "SELECT input_tokens, cached_input_tokens, output_tokens,"
            " cum_input_tokens, cum_cached_input_tokens, cum_output_tokens, usd"
            " FROM turn_costs WHERE agent = 'a1' ORDER BY id DESC LIMIT 1")
        return rows[0]

    @staticmethod
    def _usd(delta: list[int]) -> float:
        uncached = max(delta[0] - delta[1], 0)
        return (uncached * 1.25 + delta[1] * 0.125 + delta[2] * 10.0) / 1e6

    def test_tour_long_interrompu_compte_au_journal(self):
        # Le tour précédent laisse un cumul ; le nôtre commence à 2000 et
        # porte deux appels. Aucun `turn.completed` dans le flux.
        _journal(self.codex, [
            _task(1000),
            _token(_tokens(100, 20, 10), _tokens(100, 20, 10)),
            _task(2000),
            _token(_tokens(60_000, 50_000, 500), _tokens(60_100, 50_020, 510)),
            _token(_tokens(61_000, 50_000, 400), _tokens(121_100, 100_020, 910)),
        ])
        self.write_events("a1", {"type": "thread.started", "thread_id": THREAD},
                          {"type": "item.completed",
                           "item": {"type": "command_execution", "exit_code": 0}})

        usage = self.book().record(
            "a1", start=0, session=THREAD, model="gpt-6.1-sol",
            started_at=1995, turn_id="tour-7494")

        # total du fil (121100/100020/910) - repère (100/20/10).
        delta = [121_000, 100_000, 900]
        self.assertEqual([usage.input_tokens, usage.cached_input_tokens,
                          usage.output_tokens], delta)
        self.assertAlmostEqual(usage.usd, self._usd(delta), places=9)
        self.assertEqual(usage.cum_usage, [121_100, 100_020, 910])
        row = self._row()
        self.assertEqual((row["input_tokens"], row["cached_input_tokens"],
                          row["output_tokens"]), tuple(delta))
        self.assertEqual((row["cum_input_tokens"], row["cum_cached_input_tokens"],
                          row["cum_output_tokens"]), (121_100, 100_020, 910))
        self.assertGreater(float(row["usd"]), 0.0)

    def test_tour_suivant_ne_double_pas_le_cumul(self):
        _journal(self.codex, [
            _task(1000),
            _token(_tokens(100, 20, 10), _tokens(100, 20, 10)),
            _task(2000),
            _token(_tokens(61_000, 50_000, 400), _tokens(121_100, 100_020, 910)),
        ])
        self.write_events("a1", {"type": "thread.started", "thread_id": THREAD})
        book = self.book()
        book.record("a1", start=0, session=THREAD, model="gpt-6.1-sol",
                    started_at=1995, turn_id="tour-7494")
        # Le tour suivant publie le cumul du fil : la différence avec la ligne
        # du tour interrompu est le seul usage à compter.
        self.write_events(
            "a1", {"type": "turn.completed",
                   "usage": _tokens(130_100, 100_020, 1_000)})
        second = book.record("a1", start=1, session=THREAD,
                             model="gpt-6.1-sol", started_at=2020, ended=2030)
        self.assertEqual([second.input_tokens, second.cached_input_tokens,
                          second.output_tokens], [9_000, 0, 90])
        self.assertAlmostEqual(second.usd, self._usd([9_000, 0, 90]), places=9)
        row = self._row()
        self.assertEqual((row["cum_input_tokens"], row["cum_cached_input_tokens"],
                          row["cum_output_tokens"]), (130_100, 100_020, 1_000))

    def test_sans_journal_reste_zero(self):
        self.write_events("a1", {"type": "thread.started", "thread_id": THREAD})
        usage = self.book().record("a1", start=0, session=THREAD,
                                   model="gpt-6.1-sol", started_at=1995,
                                   ended=2010, turn_id="tour-7494")
        self.assertEqual([usage.input_tokens, usage.cached_input_tokens,
                          usage.output_tokens], [0, 0, 0])
        self.assertEqual(usage.usd, 0.0)

    def test_journal_d_un_tour_precedent_n_est_pas_pris(self):
        _journal(self.codex, [
            _task(1000),
            _token(_tokens(50_000, 40_000, 500), _tokens(50_000, 40_000, 500)),
        ])
        self.write_events("a1", {"type": "thread.started", "thread_id": THREAD})
        usage = self.book().record("a1", start=0, session=THREAD,
                                   model="gpt-6.1-sol", started_at=1995,
                                   ended=2010, turn_id="tour-7494")
        self.assertEqual([usage.input_tokens, usage.cached_input_tokens,
                          usage.output_tokens], [0, 0, 0])

    def test_sans_cumul_du_fil_la_somme_des_appels_suffit(self):
        # Journal sans `total_token_usage` : le minorant exact reste la somme
        # des `last_token_usage` du tour ; aucun cumul n'est avancé.
        _journal(self.codex, [
            _task(2000),
            _token(_tokens(60_000, 50_000, 500)),
            _token(_tokens(61_000, 50_000, 400)),
        ])
        self.write_events("a1", {"type": "thread.started", "thread_id": THREAD})
        usage = self.book().record("a1", start=0, session=THREAD,
                                   model="gpt-6.1-sol", started_at=1995,
                                   ended=2010, turn_id="tour-7494")
        delta = [121_000, 100_000, 900]
        self.assertEqual([usage.input_tokens, usage.cached_input_tokens,
                          usage.output_tokens], delta)
        self.assertIsNone(usage.cum_usage)
        row = self._row()
        self.assertIsNone(row["cum_input_tokens"])

    def test_nouveau_fil_apres_rotation_compte_le_tour_entier(self):
        # Cas du tour 85007 : session rotationnée au début du tour, donc pas
        # de repère avant ; le cumul du fil est celui du tour.
        _journal(self.codex, [
            _task(2000),
            _token(_tokens(193_087, 190_000, 624), _tokens(193_087, 190_000, 624)),
            _token(_tokens(195_179, 192_000, 700), _tokens(388_266, 382_000, 1_324)),
        ])
        self.write_events("a1", {"type": "thread.started", "thread_id": THREAD})
        usage = self.book().record("a1", start=0, session=THREAD,
                                   model="gpt-6.1-sol", started_at=1995,
                                   turn_id="tour-7494")
        self.assertEqual([usage.input_tokens, usage.cached_input_tokens,
                          usage.output_tokens], [388_266, 382_000, 1_324])
        self.assertEqual(usage.cum_usage, [388_266, 382_000, 1_324])

    def test_cumul_du_fil_sans_dernier_appel_compte_quand_meme(self):
        # Le dernier `token_count` porte le cumul mais pas son
        # `last_token_usage` : le delta du cumul reste exact.
        _journal(self.codex, [
            _task(1000),
            _token(_tokens(100, 20, 10), _tokens(100, 20, 10)),
            _task(2000),
            _token(_tokens(61_000, 50_000, 400), _tokens(61_100, 50_020, 410)),
            _event({"type": "token_count", "info": {
                "total_token_usage": _tokens(121_100, 100_020, 910)}}),
        ])
        self.write_events("a1", {"type": "thread.started", "thread_id": THREAD})
        usage = self.book().record("a1", start=0, session=THREAD,
                                   model="gpt-6.1-sol", started_at=1995,
                                   turn_id="tour-7494")
        self.assertEqual([usage.input_tokens, usage.cached_input_tokens,
                          usage.output_tokens], [121_000, 100_000, 900])
        self.assertEqual(usage.cum_usage, [121_100, 100_020, 910])

    def test_turn_completed_du_flux_prime_sur_le_journal(self):
        # Le journal d'un tour interrompu ne doit jamais doubler un tour dont
        # le flux a publié son usage.
        _journal(self.codex, [
            _task(2000),
            _token(_tokens(60_000, 50_000, 500), _tokens(60_000, 50_000, 500)),
        ])
        self.write_events("a1", {"type": "turn.completed",
                                 "usage": _tokens(1_000, 400, 100)})
        usage = self.book().record("a1", start=0, session=THREAD,
                                   model="gpt-6.1-sol", started_at=1995,
                                   ended=2010, turn_id="tour-7494")
        self.assertEqual([usage.input_tokens, usage.cached_input_tokens,
                          usage.output_tokens], [1_000, 400, 100])

    def test_reparation_tardive_ne_prend_pas_le_tour_suivant(self):
        # La ligne de ressources borne la fin réelle (2010) : le tour 3000,
        # postérieur, ne prête jamais son usage à ce tour.
        _journal(self.codex, [
            _task(2000),
            _token(_tokens(60_000, 50_000, 500), _tokens(60_000, 50_000, 500)),
            _task(3000),
            _token(_tokens(70_000, 60_000, 600), _tokens(130_000, 110_000, 1_100)),
        ])
        self._resource("tour-7494", ended=2010)
        self.write_events("a1", {"type": "thread.started", "thread_id": THREAD})
        usage = self.book().record("a1", start=0, session=THREAD,
                                   model="gpt-6.1-sol", started_at=1995,
                                   turn_id="tour-7494")
        self.assertEqual([usage.input_tokens, usage.cached_input_tokens,
                          usage.output_tokens], [0, 0, 0])


class CodexTurnCapEndToEndTest(PgTestCase):
    """Le tour réel du banc, clos par la borne de durée, compte ses jetons.

    Reproduit le cas 85007 : `codex exec --json` muet après son démarrage,
    arrêté par la borne dure avant `turn.completed`, journal de session déjà
    écrit. La ligne `turn_costs` doit porter l'usage du journal.
    """

    def setUp(self) -> None:
        super().setUp()
        self.db.execute("TRUNCATE turn_costs, spend_pending CASCADE")
        self.sessions = os.path.join(self.tmp, "codex-sessions")

    def test_tour_clos_sur_la_borne_de_duree_compte_au_journal(self):
        runner = Runner(self.cfg, self.db, once=True)
        cwd = os.path.join(self.tmp, "work", "cx-cap")
        os.makedirs(cwd, exist_ok=True)
        self.register("cx-cap", "codex", cwd=cwd)
        # Le harnais muet n'annonce pas son fil : la session vient du registre,
        # comme pour un tour repris.
        registry.set_session(self.db, "cx-cap", "codex-thread-1")
        storage.of(self.db).operations.set_settings(
            "cx-cap", {"turn_max_seconds": "1"})
        lease = registry.claim(self.db, "cx-cap", runner.runner_id, 3600)
        self.assertIsNotNone(lease)
        worker = AgentWorker(runner, registry.get(self.db, "cx-cap"), lease)
        self.addCleanup(worker.watchdog_stop.set)

        env = {
            "AMEESH_BIN_DIR": FAKEBIN,
            "AMEESH_TEST_LOG": self.turns_log,
            "AMEESH_CODEX_SESSIONS": self.sessions,
            # Le harnais annonce son fil puis se tait : la borne dure le tue
            # avant son `turn.completed`; son journal du tour est déjà écrit.
            "AMEESH_TEST_SLEEP_AFTER_START": "8",
            "AMEESH_TEST_CODEX_JOURNAL": self.sessions,
            "AMEESH_TEST_CODEX_LAST_USAGE": json.dumps(_tokens(61_000, 1_000, 500)),
        }
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.multiple(
                runner_mod, TURN_HARD_MARGIN_MIN_S=1.0,
                TURN_HARD_MARGIN_RATIO=0.0):
            self.assertTrue(worker.run_turn({"kind": "prompt",
                                             "prompt": "travail", "ids": []}))

        self.assertIn("hard limit", worker.last_turn_closed or "")
        rows = self.db.query(
            "SELECT input_tokens, cached_input_tokens, output_tokens, usd"
            " FROM turn_costs WHERE agent = 'cx-cap'")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["input_tokens"], rows[0]["cached_input_tokens"],
                          rows[0]["output_tokens"]), (61_000, 1_000, 500))
        self.assertGreater(float(rows[0]["usd"]), 0.0)


if __name__ == "__main__":
    unittest.main()
