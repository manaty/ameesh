# SPDX-License-Identifier: AGPL-3.0-only
"""L80b (#288) : entrées servies sur la ligne du grand livre, coût par message.

Le tour résident peut servir plusieurs messages (consigne groupée, steer) ;
son coût est **celui du tour**, partagé par ses entrées — jamais divisé ni
compté deux fois. La ligne `turn_costs` porte les identifiants des messages
réellement servis (`inputs`), et `cost turns --message` les relit.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import cost as cost_mod
from ameesh import db as db_mod
from ameesh import resident, resident_runner, storage

from .support import PgTestCase
from .test_cost import Sandbox, U


class UsageEventInputsTest(unittest.TestCase):
    def test_inputs_are_carried_and_never_fabricated(self):
        result = resident.TurnResult(
            status=resident.COMPLETED, session="s1",
            usage={"input_tokens": 3})
        event = resident_runner.usage_event(
            "acp", result, turn="t1", inputs=("12", "human:7"))
        self.assertEqual(event["inputs"], ["12", "human:7"])
        plain = resident_runner.usage_event("acp", result, turn="t1")
        self.assertNotIn("inputs", plain)   # pas de [] parasite

    def test_native_protocol_gets_a_separate_inputs_event(self):
        # L80b B1 : le flux Claude porte déjà l'usage, donc pas de
        # `resident.turn` ; les messages servis partent dans leur propre
        # événement, lu par le grand livre pour l'attribution seule.
        result = resident.TurnResult(status=resident.COMPLETED, session="s1")
        self.assertIsNone(resident_runner.usage_event(
            "claude-stream-json", result, inputs=("12",)))
        event = resident_runner.inputs_event(
            "claude-stream-json", result, turn="t1", inputs=("12", "human:7"))
        self.assertEqual(event["type"], resident.INPUTS_EVENT)
        self.assertEqual(event["inputs"], ["12", "human:7"])
        self.assertNotIn("usage", event)
        self.assertIsNone(resident_runner.inputs_event("acp", result, inputs=("12",)))
        self.assertIsNone(resident_runner.inputs_event("claude-stream-json", result))


class ServedMessageIdsTest(unittest.TestCase):
    """Le calcul des entrées servies par un tour (spec + steers acceptés)."""

    def _worker(self):
        from ameesh.runner import AgentWorker
        worker = AgentWorker.__new__(AgentWorker)
        worker.lock = threading.Lock()
        worker._turn_generation = 3
        worker._steer_pending = []
        return worker

    @staticmethod
    def _turn(status, inputs):
        return SimpleNamespace(result=SimpleNamespace(status=status, inputs=inputs))

    def test_spec_messages_and_accepted_steers_only(self):
        worker = self._worker()
        accepted = self._turn(resident.COMPLETED, ("u1",))
        failed = self._turn(resident.FAILED, ("u2",))
        earlier = self._turn(resident.COMPLETED, ("u3",))
        worker._steer_pending = [
            ("j1", [21], 3, accepted, "u1"),   # servi
            ("j2", [22], 3, failed, "u2"),     # tour non achevé : libéré
            ("j3", [23], 2, earlier, "u3"),    # tour précédent
        ]
        self.assertEqual(worker._served_message_ids([7, 8]), ("21", "7", "8"))

    def test_no_input_no_ids(self):
        self.assertEqual(self._worker()._served_message_ids(), ())


class ResidentInputsBoundaryTest(unittest.TestCase):
    """L80b B2 : la capture des entrées suit la barrière steer du tour.

    L'ancienne capture lisait `_steer_pending` avant la jonction du moniteur et
    la barrière `_steer_lock` : un ACK de steer accepté pendant la clôture
    n'était jamais compté. Ici le steer tient la barrière, l'ancien point de
    capture ne le voit pas ; `_resident_turn_end` attend, puis
    `_resident_usage_final` l'inclut dans l'événement écrit."""

    def _worker(self, tmp):
        from ameesh.runner import AgentWorker
        worker = AgentWorker.__new__(AgentWorker)
        threading.Thread.__init__(worker)  # machine à états du fil, jamais démarrée
        worker.name = "w"
        worker.lock = threading.Lock()
        worker._steer_lock = threading.Lock()
        worker._turn_generation = 7
        worker._turn_live = True
        worker._steer_pending = []
        worker._turn_id = "tour-1"
        worker.proc = None
        worker.pgid = None
        worker.stopping = threading.Event()
        worker.restarting = threading.Event()
        worker.host_yield = None
        worker._power_texte = ""
        worker._urgent_end = lambda: ""
        worker._path = lambda name: os.path.join(tmp, name)
        return worker

    @staticmethod
    def _rp():
        return SimpleNamespace(
            alive=True, busy=False, turns=0, last_used=0.0, session="s1",
            closing=False, mark_alive=lambda: None,
            driver=SimpleNamespace(protocol="claude-stream-json", busy=False))

    def _tmp(self):
        tmp = tempfile.mkdtemp(prefix="ameesh-288-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def test_a_steer_accepted_while_the_turn_closes_is_counted(self):
        tmp = self._tmp()
        worker = self._worker(tmp)
        steer = SimpleNamespace(result=SimpleNamespace(
            status=resident.COMPLETED, inputs=("u1",)))
        ready = threading.Event()
        release = threading.Event()

        def ack():
            with worker._steer_lock:       # le steer en vol tient la barrière
                ready.set()
                release.wait(2)
                with worker.lock:          # ACK accepté pendant la clôture
                    worker._steer_pending.append(("j1", [21], 7, steer, "u1"))

        thread = threading.Thread(target=ack)
        thread.start()
        try:
            self.assertTrue(ready.wait(2))
            # capture d'avant la barrière (l'ancien point) : le steer manque
            self.assertEqual(worker._served_message_ids([7]), ("7",))
            release.set()
            worker._resident_turn_end(self._rp())
            fin = SimpleNamespace(status=resident.COMPLETED, session="s1")
            self.assertEqual(worker._resident_usage_final(
                self._rp(), fin, [7]), (True, None))
        finally:
            release.set()
            thread.join(2)
        with open(os.path.join(tmp, "events.jsonl"), encoding="utf-8") as fh:
            event = [json.loads(line) for line in fh][-1]
        self.assertEqual(event["type"], resident.INPUTS_EVENT)
        self.assertEqual(event["inputs"], ["21", "7"])

    def test_native_inputs_write_failure_does_not_fail_the_turn(self):
        from unittest import mock
        tmp = self._tmp()
        worker = self._worker(tmp)
        fin = SimpleNamespace(status=resident.COMPLETED, session="s1")
        with mock.patch("ameesh.resident_runner.write_usage_event",
                        return_value=False):
            self.assertEqual(worker._resident_usage_final(
                self._rp(), fin, [7]), (True, None))


class InputsAttributionTest(unittest.TestCase):
    def test_un_tour_non_resident_attribue_aussi_ses_messages(self):
        """#3452 : le même événement d'attribution sert un tour non résident
        (harnais CLI) — `cost turns --message` ne rendait rien pour eux."""
        event = resident_runner.inputs_attribution(
            "deepseek", session="s1", turn="t1", inputs=("12", "human:7"),
            status="completed")
        self.assertEqual(event["type"], resident.INPUTS_EVENT)
        self.assertEqual(event["protocol"], "deepseek")
        self.assertEqual(event["inputs"], ["12", "human:7"])
        self.assertEqual(event["session"], "s1")
        self.assertIsNone(resident_runner.inputs_attribution(
            "deepseek", session="s1", inputs=()))


class TurnCostsInputsTest(Sandbox, PgTestCase):
    """L'écriture (record → turn_costs), la lecture par message et la CLI."""

    def _resident_event(self, inputs=None):
        event = {
            "type": resident.USAGE_EVENT, "protocol": "codex-app-server",
            "session": "t1", "turn": "tour-1", "status": "completed",
            "usage": {"input_tokens": 100, "cache_read_input_tokens": 0,
                      "output_tokens": 10},
            "usage_total": {"input_tokens": 100, "cached_input_tokens": 0,
                            "output_tokens": 10},
        }
        if inputs is not None:
            event["inputs"] = list(inputs)
        return event

    def _record(self, inputs=None, start=0):
        self.agent("a1", "codex", "gpt-6.1-sol")
        book = self.book()
        self.events("a1", self._resident_event(inputs))
        return book.record("a1", start=start)

    def test_inputs_are_written_on_the_ledger_line(self):
        self._record(["12", "human:7"])
        row = self.db.query(
            "select inputs from turn_costs where agent = 'a1'")[0]
        self.assertEqual(row["inputs"], ["12", "human:7"])

    def test_without_inputs_the_column_stays_null(self):
        self._record()
        row = self.db.query(
            "select inputs from turn_costs where agent = 'a1'")[0]
        self.assertIsNone(row["inputs"])

    def test_claude_inputs_event_attributes_its_stream_turn(self):
        # L80b B1 : le tour résident Claude n'écrit pas `resident.turn` (son
        # flux porte l'usage) ; `resident.inputs` donne l'attribution, sans
        # toucher au coût lu dans le flux.
        self.agent("a1", "claude")
        book = self.book()
        self.events(
            "a1",
            {"type": "result", "session_id": "s1", "total_cost_usd": 0.5,
             "usage": U},
            {"type": resident.INPUTS_EVENT, "protocol": "claude-stream-json",
             "session": "s1", "turn": "tour-1", "status": "completed",
             "inputs": ["12", "13"]})
        usage = book.record("a1")
        self.assertAlmostEqual(usage.usd, 0.5, places=9)
        row = self.db.query(
            "select inputs from turn_costs where agent = 'a1'")[0]
        self.assertEqual(row["inputs"], ["12", "13"])
        rows = storage.of(self.db).operations.turns(
            agent="a1", since_s=3600, limit=50, message="12")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["inputs"], ["12", "13"])

    def test_un_evenement_non_resident_est_lu_par_le_grand_livre(self):
        """#3452 : la ligne du grand livre relit l'attribution d'un tour non
        résident (même événement, autre protocole), sans mesure résidente."""
        self.agent("a1", "deepseek")
        book = self.book()
        self.events(
            "a1",
            {"type": "status", "phase": "step_end",
             "usage": {"inputTokens": 1000, "cacheReadTokens": 0, "outputTokens": 10}},
            {"type": resident.INPUTS_EVENT, "protocol": "deepseek",
             "session": "s1", "turn": "t1", "status": "completed",
             "inputs": ["12"]})
        book.record("a1")
        row = self.db.query(
            "select inputs from turn_costs where agent = 'a1'")[0]
        self.assertEqual(row["inputs"], ["12"])

    def test_turns_by_message_returns_the_turn_cost_once(self):
        self._record(["12", "13"])
        self._record(["14"], start=1)     # deuxième tour, autre message
        ops = storage.of(self.db).operations
        rows = ops.turns(agent="a1", since_s=3600, limit=50, message="12")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["inputs"], ["12", "13"])
        self.assertGreater(rows[0]["usd"], 0)
        # un tour groupé n'est pas divisé : la ligne reste le coût du tour
        total = ops.turns(agent="a1", since_s=3600, limit=50)
        self.assertEqual(len(total), 2)
        self.assertEqual(ops.turns(agent="a1", since_s=3600, limit=50,
                                   message="999"), [])

    def test_cli_turns_by_message(self):
        self._record(["12"])
        proc = self.mesh("cost", "turns", "--message", "12", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual([t["inputs"] for t in data["turns"]], [["12"]])
        text = self.mesh("cost", "turns", "--message", "12")
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("turns serving message 12", text.stdout)

class MediatedInsertCompatTest(unittest.TestCase):
    """Contrat 1.7 : `inputs` facultatif ; serveur 1.6 → réessai sans attribution."""

    def _book(self):
        return cost_mod.CostBook(
            state_dir=tempfile.mkdtemp(prefix="ameesh-288-"), db=object())

    def test_inputs_omitted_when_none(self):
        calls = []
        fake = SimpleNamespace(turn_costs=SimpleNamespace(
            insert=lambda **kw: calls.append(dict(kw))))
        with mock.patch.object(cost_mod.storage, "of", return_value=fake):
            self._book()._insert_turn_cost(agent="a1", spend_key="k")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("inputs", calls[0])

    def test_server_16_bad_args_retries_without_inputs(self):
        calls = []

        def insert(**kw):
            calls.append(dict(kw))
            if "inputs" in kw:
                raise db_mod.DbError(
                    "bad_args (400) : turn_costs.insert : paramètre inconnu inputs")

        fake = SimpleNamespace(turn_costs=SimpleNamespace(insert=insert))
        with mock.patch.object(cost_mod.storage, "of", return_value=fake):
            self._book()._insert_turn_cost(
                agent="a1", spend_key="k", inputs=("12", "human:7"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["inputs"], ("12", "human:7"))
        self.assertNotIn("inputs", calls[1])

    def test_a_real_failure_is_not_swallowed(self):
        def insert(**kw):
            raise db_mod.DbError("connexion perdue")

        fake = SimpleNamespace(turn_costs=SimpleNamespace(insert=insert))
        with mock.patch.object(cost_mod.storage, "of", return_value=fake):
            with self.assertRaises(db_mod.DbError):
                self._book()._insert_turn_cost(agent="a1", inputs=("12",))
