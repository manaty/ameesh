# SPDX-License-Identifier: AGPL-3.0-only
"""#5565 — réconciliation comptable hors PostgreSQL local.

Les extensions `reconcile`/`report_ambiguous` de `PendingSpend` (lot #5561)
sont locales au stockage PostgreSQL : une connexion d'exécuteur médié ne les
porte pas. Deux défauts non bloquants relevés en revue :

* la CLI `ameesh budget reconcile` laissait échapper un `AttributeError` au
  lieu d'une erreur claire ;
* le runner taisait entièrement l'incident d'ambiguïté, faute de réclamation
  durable — au lieu de garder un signal unique par marqueur.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import threading
import unittest
from argparse import Namespace
from types import SimpleNamespace
from unittest import mock

from ameesh import budget
from ameesh.runner import AgentWorker


class ReconcileCliTest(unittest.TestCase):
    """`ameesh budget reconcile` sur un stockage sans extension."""

    def _run(self, *, pending_spend, authorize=None):
        fake_db = SimpleNamespace(close=lambda: None)
        store = SimpleNamespace(driver="mediated", pending_spend=pending_spend)
        args = Namespace(budget_command="reconcile", agent="reconcile",
                         spend_key="reconcile:2:1792540300.16036",
                         abandon=False, reason="checked accounting",
                         human="human:chef")
        err = io.StringIO()
        with mock.patch.object(budget.db_mod, "connect", return_value=fake_db), \
                mock.patch.object(budget.db_mod, "require_schema", lambda db: None), \
                mock.patch.object(budget.storage, "of", return_value=store), \
                mock.patch.object(budget, "authorize", authorize or (lambda *a: "human:chef")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = budget.cmd_budget(SimpleNamespace(), args)
        return code, err.getvalue()

    def test_clear_error_instead_of_attribute_error(self):
        """Un exécuteur médié obtient une erreur claire, pas une traceback."""
        calls = []
        code, err = self._run(pending_spend=SimpleNamespace(),
                              authorize=lambda *a: calls.append(a))
        self.assertEqual(code, 1)
        self.assertIn("unavailable on this storage", err)
        self.assertIn("mediated", err)
        self.assertIn("nothing changed", err)
        self.assertEqual(calls, [], "l'extension absente est dite avant authorize")

    def test_local_extension_still_reconciles(self):
        """Le chemin PostgreSQL local n'est pas changé par la garde."""
        seen = {}

        def reconcile(agent, **kwargs):
            seen.update(agent=agent, **kwargs)
            return {"id": 7, "event": "link"}

        code, err = self._run(pending_spend=SimpleNamespace(reconcile=reconcile))
        self.assertEqual(code, 0, err)
        self.assertEqual(seen["agent"], "reconcile")
        self.assertEqual(seen["spend_key"], "reconcile:2:1792540300.16036")
        self.assertEqual(seen["actor"], "human:chef")


class AccountingIncidentFallbackTest(unittest.TestCase):
    """Le runner garde un signal d'ambiguïté sans réclamation durable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ameesh-5565-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.notes = []
        self.marker = {"start": 2, "ledger_ambiguous": True,
                       "created_at": "2026-10-20T23:51:40.160356Z",
                       "spend_key": None}

    def _worker(self, name="reconcile"):
        worker = object.__new__(AgentWorker)
        threading.Thread.__init__(worker)
        worker.name = name
        worker.db = object()          # `storage.of` est substitué dans `_call`
        worker.state_dir = os.path.join(self.tmp, "agents", name)
        os.makedirs(worker.state_dir, exist_ok=True)
        worker._compta_en_echec = False
        worker._fil_note = lambda text, **kw: self.notes.append((text, kw))
        return worker

    def _call(self, worker, *, pending_spend):
        store = SimpleNamespace(driver="mediated", pending_spend=pending_spend)
        with mock.patch("ameesh.runner.storage.of", return_value=store):
            return worker._compta_ecrit(dict(self.marker))

    def test_absent_durable_claim_keeps_one_local_signal(self):
        """Sondage répété : un seul signal, jamais le silence."""
        worker = self._worker()
        self.assertFalse(self._call(worker, pending_spend=SimpleNamespace()))
        self.assertFalse(self._call(worker, pending_spend=SimpleNamespace()))
        self.assertTrue(worker._compta_en_echec)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("Accounting marker ambiguous", self.notes[0][0])
        self.assertEqual(self.notes[0][1]["meta"]["action"], "accounting_incident")
        self.assertEqual(worker._state_read("accounting_incident"),
                         self.marker["created_at"])

    def test_local_signal_survives_worker_restart(self):
        """Le repli local ne rejoue pas l'incident au redémarrage du worker."""
        first = self._worker()
        self._call(first, pending_spend=SimpleNamespace())
        replacement = self._worker()
        self._call(replacement, pending_spend=SimpleNamespace())
        self.assertEqual(len(self.notes), 1)

    def test_new_marker_gets_its_own_signal(self):
        """Un marqueur remplacé n'est jamais étouffé par l'ancien signal."""
        worker = self._worker()
        self._call(worker, pending_spend=SimpleNamespace())
        self.marker["created_at"] = "2026-10-21T00:00:00.000001Z"
        self._call(worker, pending_spend=SimpleNamespace())
        self.assertEqual(len(self.notes), 2)

    def test_durable_claim_is_used_when_present(self):
        """PostgreSQL local : la réclamation durable reste la seule source."""
        claims = []

        def report_ambiguous(name, marker):
            claims.append((name, marker["created_at"]))
            return len(claims) == 1

        worker = self._worker()
        api = SimpleNamespace(report_ambiguous=report_ambiguous)
        self._call(worker, pending_spend=api)
        self._call(worker, pending_spend=api)
        self.assertEqual(len(claims), 2)
        self.assertEqual(len(self.notes), 1, "deuxième sondage déjà réclamé")
        self.assertEqual(worker._state_read("accounting_incident"), "",
                         "le repli local ne double pas la réclamation durable")


if __name__ == "__main__":
    unittest.main()
