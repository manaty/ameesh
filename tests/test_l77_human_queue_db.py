# SPDX-License-Identifier: AGPL-3.0-only
"""L77 (#286): trace L78 des messages humains servis — base réelle.

Le journal ne porte aucun état servi/ack : l'entrée `human_message` est écrite
à la REMISE, avec `event_id = mail:<id>` pour qu'une remise incertaine rejouée
ne duplique pas la trace.
"""
from __future__ import annotations

import dataclasses
import os

from ameesh import conversation, registry
from ameesh.runner import AgentWorker, Runner

from .support import PgTestCase


def _make_worker(test, name, prompt=None):
    cwd = os.path.join(test.tmp, "work", name)
    os.makedirs(cwd, exist_ok=True)
    kwargs = {"prompt": prompt} if prompt is not None else {}
    test.register(name, "claude", cwd=cwd, **kwargs)
    runner = Runner(dataclasses.replace(test.cfg), test.db, once=True)
    lease = registry.claim(test.db, name, runner.runner_id, 3600)
    test.assertIsNotNone(lease)
    worker = AgentWorker(runner, registry.get(test.db, name), lease)
    test.addCleanup(worker.watchdog_stop.set)
    test.addCleanup(worker._close_peek)
    return worker


class HumanJournalTest(PgTestCase):
    def _worker(self, name="worker"):
        return _make_worker(self, name)

    def _spec(self, mid=11, sender="human:alice", body="hello"):
        return {"human": True,
                "rows": [{"id": mid, "sender": sender, "body": body,
                          "payload": {"human": True}, "created_ts": 1.0}]}

    def _human_entries(self, name):
        page = conversation.history(self.db, name, limit=50)
        return [e for e in page["entries"] if e["kind"] == "human_message"]

    def test_human_message_journaled_once_per_delivery(self):
        name = "worker-once"
        worker = self._worker(name)
        worker._journal_humans(self._spec(), [11])
        # Une remise incertaine rejouée ne duplique pas l'entrée.
        worker._journal_humans(self._spec(), [11])
        entries = self._human_entries(name)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["author"], "human:alice")
        self.assertEqual(entries[0]["text"], "hello")
        self.assertEqual(entries[0]["event_id"], "mail:11")

    def test_bare_human_name_is_prefixed(self):
        name = "worker-bare"
        worker = self._worker(name)
        worker._journal_humans(self._spec(mid=12, sender="alice"), [12])
        entries = self._human_entries(name)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["author"], "human:alice")

    def test_non_human_spec_writes_nothing(self):
        name = "worker-plain"
        worker = self._worker(name)
        spec = self._spec()
        spec["human"] = False
        worker._journal_humans(spec, [11])
        self.assertEqual(self._human_entries(name), [])


class PickHumanFirstTest(PgTestCase):
    """L77 : `_pick` sert l'humain d'abord et ne le consomme pas.

    At-least-once : la construction du spec ne réserve ni ne remet rien ;
    seule la remise (lancement du harnais) solde la réservation.
    """

    def test_pick_serves_human_first_without_delivering(self):
        from ameesh import mail
        name = "worker-pick"
        worker = _make_worker(self, name)
        mail.send(self.db, "human:alice", name, "from a human",
                  payload={"human": True})
        mail.send(self.db, "agent:bob", name, "from an agent", payload={})
        spec = worker._pick()
        self.assertIsNotNone(spec)
        self.assertEqual(spec["kind"], "mail")
        self.assertTrue(spec.get("human"))
        self.assertEqual(len(spec["ids"]), 1)
        row = mail.get(self.db, spec["ids"][0])
        self.assertEqual(row["sender"], "human:alice")
        self.assertIsNone(row.get("delivered_at"))
        # Re-pick : la construction du spec n'a rien consommé.
        again = worker._pick()
        self.assertIsNotNone(again)
        self.assertEqual(again["ids"], spec["ids"])
