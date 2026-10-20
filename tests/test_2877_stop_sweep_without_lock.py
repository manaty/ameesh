# SPDX-License-Identifier: AGPL-3.0-only
"""#2877 : la passe du runner sur les agents arrêtés ne verrouille le registre
que s'il y a un lot à rendre.

`release_stopped_lots_once` ouvrait, à chaque passe et dans chaque exécuteur,
la transaction de `release_lots` pour chaque agent arrêté délibérément — verrou
de sa ligne `agent_registry` d'abord — même quand il n'avait plus rien. Le
20/10, seize exécuteurs le faisaient pour ameesh-dev6 (arrêté, aucun lot) :
une trentaine de sessions en file sur « SELECT status, stop_reason FROM
agent_registry WHERE name = $1 FOR NO KEY UPDATE ». Un relevé sans verrou
(`work.held_lots`) dit désormais qui tient encore quelque chose ; seuls
ceux-là passent par la transaction.
"""
from __future__ import annotations

import threading
import types
from unittest import mock

from ameesh import registry, work
from ameesh import runner as runner_mod

from .support import PgTestCase


class _Base(PgTestCase):
    """Mêmes fabriques que `tests/test_1425_stop_sweep_suites.py`."""

    def agent(self, name, *, status="idle", team="nexlink"):
        registry.upsert(self.db, name, harness="deepseek", host=self.cfg.host)
        self.db.execute(
            "UPDATE agent_registry SET status = %s, team = %s,"
            " responsible = 'human:proprio' WHERE name = %s",
            (status, team, name))

    def lot(self, title, *, assignee=None):
        item = work.add(self.db, title=title, app="nexlink")
        if assignee:
            work.assign(self.db, item["id"], assignee, cfg=self.cfg,
                        force="test: fixture, lots multiples (#540)")
        return work.get(self.db, item["id"])

    def promoted(self, assignee):
        lot = self.lot("deployed", assignee=assignee)
        work.close_merged(self.db, lot["id"], sha="a" * 40, actor=assignee)
        work.move(self.db, lot["id"], "promoted", note="déployé", actor="auditeur")
        return lot

    def stopped(self, name, reason="manuel"):
        registry.set_status(self.db, name, "stopped", status_text="stopped",
                            stop_reason=reason)

    def sweep_runner(self, db=None):
        return types.SimpleNamespace(
            mediated=False, db=db or self.db, cfg=self.cfg, runner_id="runner-test",
            dry_run=False)


class HeldLotsTest(_Base):
    def test_open_and_promoted_per_agent(self):
        self.agent("dev1")
        self.agent("dev2")
        self.agent("dev3")
        self.lot("A", assignee="dev1")
        self.promoted("dev2")
        closed = self.lot("C", assignee="dev3")
        work.close(self.db, closed["id"], abandoned=True, note="abandon", actor="dev3")
        merged = self.lot("M", assignee="dev3")
        work.close_merged(self.db, merged["id"], sha="b" * 40, actor="dev3")
        self.assertEqual(work.held_lots(self.db, ["dev1", "dev2", "dev3", "inconnu"]),
                         {"dev1": {"open": True, "promoted": False},
                          "dev2": {"open": False, "promoted": True}})

    def test_no_name_no_read(self):
        with mock.patch.object(self.db, "query",
                               side_effect=AssertionError("lecture sans nom")):
            self.assertEqual(work.held_lots(self.db, ["", "  "]), {})


class SweepLocksOnlyHoldersTest(_Base):
    def test_stopped_agent_holding_nothing_is_not_locked(self):
        self.agent("dev6")
        self.stopped("dev6")
        with mock.patch.object(work, "release_lots",
                               side_effect=AssertionError("transaction sans lot")), \
                mock.patch.object(work, "configured_canons",
                                  side_effect=AssertionError("canons lus sans lot")):
            self.assertEqual(
                runner_mod.Runner.release_stopped_lots_once(self.sweep_runner()), [])

    def test_only_the_holder_goes_through_the_transaction(self):
        self.agent("dev1")
        self.agent("dev6")
        lot = self.lot("A", assignee="dev1")
        self.stopped("dev1")
        self.stopped("dev6")
        with mock.patch.object(work, "release_lots", wraps=work.release_lots) as spy:
            done = runner_mod.Runner.release_stopped_lots_once(self.sweep_runner())
        self.assertEqual([call.args[1] for call in spy.call_args_list], ["dev1"])
        self.assertEqual([row["id"] for row in done], [lot["id"]])
        self.assertIsNone(work.get(self.db, lot["id"])["assignee"])

    def test_promoted_lot_without_measure_actor_is_not_locked(self):
        self.agent("dev1")                       # pas d'auditeur : personne pour mesurer
        lot = self.promoted("dev1")
        self.stopped("dev1")
        with mock.patch.object(work, "release_lots",
                               side_effect=AssertionError("transaction sans destinataire")):
            self.assertEqual(
                runner_mod.Runner.release_stopped_lots_once(self.sweep_runner()), [])
        self.assertEqual(work.get(self.db, lot["id"])["assignee"], "dev1")

    def test_locked_registry_row_does_not_hold_the_pass(self):
        """Le convoi du 20/10 : la ligne de l'agent arrêté est verrouillée par
        une autre session ; la passe, qui n'a rien à lui rendre, n'attend pas."""
        if self.db.name != "psycopg":
            self.skipTest("row-lock contention requires a persistent connection")
        self.agent("dev6")
        self.stopped("dev6")
        blocker = self.connect()
        other = self.connect()
        result, errors = [], []

        def sweep():
            try:
                result.append(runner_mod.Runner.release_stopped_lots_once(
                    self.sweep_runner(other)))
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=sweep)
        try:
            blocker.execute("BEGIN")
            blocker.query("SELECT name FROM agent_registry WHERE name = 'dev6' FOR UPDATE")
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "la passe attend le verrou du registre")
            self.assertEqual((errors, result), ([], [[]]))
        finally:
            blocker.execute("ROLLBACK")
            worker.join(timeout=5)
            other.close()
            blocker.close()
