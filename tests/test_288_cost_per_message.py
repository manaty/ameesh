# SPDX-License-Identifier: AGPL-3.0-only
"""L80b (#288) : entrées servies sur la ligne du grand livre, coût par message.

Le tour résident peut servir plusieurs messages (consigne groupée, steer) ;
son coût est **celui du tour**, partagé par ses entrées — jamais divisé ni
compté deux fois. La ligne `turn_costs` porte les identifiants des messages
réellement servis (`inputs`), et `cost turns --message` les relit.
"""
from __future__ import annotations

import json
import threading
import unittest
from types import SimpleNamespace

from ameesh import resident, resident_runner, storage

from .support import PgTestCase
from .test_cost import Sandbox


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
