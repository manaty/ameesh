# SPDX-License-Identifier: AGPL-3.0-only
"""Consumer probe for #1467/#1680/#1374; real Git/SQL, fixture CI/provenance.

Loads only exact supplier Git objects. Boundary witnesses and the actual
Passage/Service adapter run separately. Durations below are test fixtures,
never live policy.
"""
import argparse
import ast
import dataclasses
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ameesh import (db as db_mod, land, train, train_fire, train_reservation,
                    train_runner, train_runtime, work)
from ameesh.storage.postgres import work as postgres_work
from tests.test_1467_train_runner import TrainRunnerTest


class ReservationConsumerTest(TrainRunnerTest):
    def prepare(self):
        """Stop before publication, persist the adopted ticket, then resume."""
        passage = self.passage()
        with mock.patch.object(train_runtime.Runtime, "publish",
                               side_effect=train.TrainError("probe: before publication")):
            result = passage.step(self.db, self.now)
        self.assertEqual(result["result"], "publication-uncertain", result)
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)",
                                  "refs/heads/integration/"), "")
        runtime = passage.runtime(self.db)
        plan = runtime.ledger.load()["plan"]
        ticket = reservation.begin(self.db, self.project, "origin/main",
            owner=runtime.ledger.owner, epoch=passage.epoch,
            branch=plan["branch"], base=plan["base"], head=plan["head"],
            ci_contract=train.digest([dataclasses.asdict(w) for w in self.workflows]),
            deadline=self.now + 3600, now=self.now, ttl_s=90)
        self.assertEqual((ticket.key, ticket.owner, ticket.epoch),
                         (runtime.ledger.key, runtime.ledger.owner, passage.epoch))
        self.persist(runtime, ticket)
        return passage, ticket, plan

    def persist(self, runtime, ticket):
        state = runtime.ledger.load()
        state["reservation"] = ticket.to_json()
        return runtime.ledger.save(state, state["revision"], ticket.epoch)

    def fresh(self):
        connection = db_mod.connect(self.__class__.cfg)
        self.addCleanup(connection.close)
        return connection

    def expire(self, ticket):
        self.db.execute("UPDATE merge_locks SET expires_at = clock_timestamp() - interval '1 second'"
                        " WHERE target = %s", (ticket.key,))

    def test_adopt_before_publication_and_resume_on_fresh_connection(self):
        passage, ticket, plan = self.prepare()
        other = self.fresh()
        runtime = passage.runtime(other)
        saved = reservation.Ticket.from_json(runtime.ledger.load()["reservation"])
        self.assertEqual(saved, ticket)
        result = passage.step(other, self.now + 1)
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual(passage.epoch, ticket.epoch)
        self.assertEqual(self.git(self.origin, "rev-parse", plan["branch"]), plan["head"])
        self.assertEqual(runtime.ledger.load()["reservation"], ticket.to_json())
        self.assertIsNone(runtime.ledger.ops.land_lock(ticket.key, "direct-writer", 90))

    def test_maintenance_is_sql_only_and_preserves_epoch_and_plan(self):
        passage, ticket, plan = self.prepare()
        connection = self.fresh()
        runtime = passage.runtime(connection)
        calls = list(self.api_calls)
        with mock.patch.object(train_runner.Passage, "step", side_effect=AssertionError("passage")), \
                mock.patch.object(train_runtime.Runtime, "ci", side_effect=AssertionError("CI read")):
            for seconds in (30, 60, 90, 120):
                ticket = reservation.renew(connection, ticket, now=self.now + seconds)
                self.persist(runtime, ticket)
                self.assertEqual(ticket.epoch, passage.epoch)
                self.assertIsNone(runtime.ledger.ops.land_lock(ticket.key, "direct-writer", 90))
        self.assertEqual(self.api_calls, calls)
        self.assertEqual(runtime.ledger.load()["plan"], plan)
        self.assertEqual(reservation.next_renew(ticket), self.now + 150)

    def test_complete_green_keeps_reservation_until_real_landing(self):
        passage, ticket, plan = self.prepare()
        connection = self.fresh()
        self.assertEqual(passage.step(connection, self.now + 1)["result"], "ci")
        runtime = passage.runtime(connection)

        def read(observed):
            evidence = runtime.ci(plan)
            return dict(branch=plan["branch"], base=plan["base"], head=evidence["head"],
                        ci_contract=observed.ci_contract, kind="passed",
                        complete=evidence["complete"] and evidence["status"] == "green")

        retained = reservation.observe(connection, ticket, now=self.now + 2, read=read)
        self.assertEqual(retained["state"], "retained")
        self.assertIsNone(runtime.ledger.ops.land_lock(ticket.key, "direct-writer", 90))
        with mock.patch.object(train_fire.Fires, "_still_pilot", return_value=True):
            with mock.patch.object(work, "corrector", return_value="pilot"):
                runtime.fires.give(plan, actor="pilot")
            result = passage.step(connection, self.now + 3)
        self.assertEqual(result["result"], "landed", result)
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), plan["head"])
        self.assertIsNone(passage.epoch)
        self.assertIsNotNone(runtime.ledger.ops.land_lock(ticket.key, "direct-writer", 90))

    def test_expired_epoch_and_absolute_deadline_do_not_authorize_publication(self):
        passage, ticket, plan = self.prepare()
        self.expire(ticket)
        with self.assertRaises(reservation.ReservationError):
            reservation.renew(self.db, ticket, now=self.now + 30)
        self.assertEqual(passage.step(self.db, self.now + 30), {"result": "busy"})
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)",
                                  "refs/heads/integration/"), "")
        with self.assertRaisesRegex(reservation.ReservationError, "absolute deadline"):
            reservation.renew(self.db, ticket, now=ticket.deadline)
        self.assertIsNotNone(self.ops.land_lock(ticket.key, "next-writer", 90))
        with self.assertRaises(reservation.ReservationError):
            reservation.renew(self.db, ticket, now=ticket.deadline + 1)

    def test_aliases_share_key_and_old_namespace_is_not_silently_replaced(self):
        _, ticket, _ = self.prepare()
        for target in ("main", "origin/main", "refs/heads/main", "refs/remotes/origin/main"):
            self.assertEqual(land.lock_key(self.project, target), ticket.key)
        with mock.patch.object(land, "lock_key", original_key):
            with self.assertRaises(reservation.ReservationError):
                reservation.begin(self.db, self.project, "origin/main", owner=ticket.owner,
                    epoch=ticket.epoch, branch=ticket.branch, base=ticket.base, head=ticket.head,
                    ci_contract=ticket.ci_contract, deadline=ticket.deadline, now=self.now, ttl_s=90)
        self.assertIsNone(self.ops.land_lock(ticket.key, "direct-writer", 90))

    def test_same_owner_acquisition_without_epoch_invalidates_the_ticket(self):
        _, ticket, _ = self.prepare()
        newer = self.ops.land_lock(ticket.key, ticket.owner, 90)
        self.assertGreater(newer, ticket.epoch)
        with self.assertRaises(reservation.ReservationError):
            reservation.renew(self.db, ticket, now=self.now + 1)
        self.assertFalse(self.ops.land_unlock(ticket.key, ticket.owner, ticket.epoch))
        self.assertEqual(self.ops.land_lock(ticket.key, ticket.owner, 90, epoch=newer), newer)


class InstalledReservationTest(TrainRunnerTest):
    """Actual Passage/ledger/Service integration against exact #1680 objects."""

    def passage(self, **kw):
        self.decision_now = getattr(self, "decision_now", self.now)
        port = train_reservation.Reservation(reservation,
            deadline=lambda plan, now: now + 3600, clock=lambda: self.decision_now)
        return super().passage(**dict(dict(reservation=port), **kw))

    def fresh(self):
        connection = db_mod.connect(self.__class__.cfg)
        self.addCleanup(connection.close)
        return connection

    def prepared(self):
        passage = self.passage()

        def inspect(runtime, plan, epoch):
            saved = runtime.ledger.load()
            ticket = reservation.Ticket.from_json(saved["reservation"])
            self.assertEqual((ticket.epoch, ticket.head), (epoch, plan["head"]))
            self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)",
                                      "refs/heads/integration/"), "")
            raise train.TrainError("probe: before publication")

        with mock.patch.object(train_runtime.Runtime, "publish", new=inspect):
            result = passage.step(self.db, self.now)
        self.assertEqual(result["result"], "publication-uncertain", result)
        saved = self.ops.train_read(self.key)
        return passage, reservation.Ticket.from_json(saved["reservation"]), saved["plan"]

    def test_ticket_precedes_push_and_new_passage_adopts_same_epoch(self):
        _, ticket, plan = self.prepared()
        restarted = self.passage()
        self.assertIsNone(restarted.epoch)
        result = restarted.step(self.fresh(), self.now + 1)
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual(restarted.epoch, ticket.epoch)
        self.assertEqual(self.git(self.origin, "rev-parse", plan["branch"]), ticket.head)
        self.assertIsNotNone(restarted.maintenance_at)

    def test_service_maintenance_is_fresh_sql_only_and_persists_ticket(self):
        passage, ticket, plan = self.prepared()
        service = train_runner.Service(self.cfg, [passage], stop=__import__("threading").Event(),
                                       report=self.fail)
        calls = list(self.api_calls)
        with mock.patch("ameesh.scoped_db.ScopedDb", side_effect=lambda cfg: self.fresh()), \
             mock.patch.object(passage, "runtime", side_effect=AssertionError("runtime")), \
             mock.patch.object(passage, "step", side_effect=AssertionError("engine")):
            due = service.maintain_once(0, self.now + 30)
        saved = self.ops.train_read(self.key)
        renewed = reservation.Ticket.from_json(saved["reservation"])
        self.assertEqual((renewed.epoch, renewed.checked_at), (ticket.epoch, self.now + 30))
        self.assertEqual(saved["plan"], plan)
        self.assertEqual(due, reservation.next_renew(renewed))
        self.assertEqual(self.api_calls, calls)
        self.assertIsNone(self.ops.land_lock(ticket.key, "direct-writer", 90))

    def test_restart_cannot_reacquire_expired_ticket_or_extend_absolute_deadline(self):
        _, ticket, plan = self.prepared()
        self.db.execute("UPDATE merge_locks SET expires_at=clock_timestamp()-interval '1 second'"
                        " WHERE target=%s", (ticket.key,))
        result = self.passage().step(self.fresh(), self.now + 1)
        self.assertEqual(result, {"result": "busy"})
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)",
                                  "refs/heads/integration/"), "")
        with self.assertRaisesRegex(train.TrainError, "absolute deadline"):
            self.passage().maintain(self.fresh(), ticket.deadline)
        self.assertIsNotNone(self.ops.land_lock(ticket.key, "next-writer", 90))

    def test_green_retains_reservation_until_landing_and_stops_maintenance(self):
        passage = self.passage()
        result = passage.step(self.db, self.now)
        self.assertEqual(result["result"], "ci", result)
        self.assertEqual(passage.step(self.fresh(), self.now + 1)["result"], "awaiting-fire")
        self.assertIsNotNone(passage.maintenance_at)
        runtime = passage.runtime(self.db)
        with mock.patch.object(work, "corrector", return_value="pilot"):
            runtime.fires.give(result["plan"], actor="pilot")
        with mock.patch.object(train_fire.Fires, "_still_pilot", return_value=True):
            self.assertEqual(passage.step(self.fresh(), self.now + 2)["result"], "landed")
        self.assertIsNone(passage.maintenance_at)
        self.assertIsNone(passage.maintain(self.fresh(), self.now + 3))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), result["plan"]["head"])

    def test_missing_or_invalid_absolute_deadline_refuses_before_publication(self):
        with self.assertRaises(train.TrainError):
            train_reservation.Reservation(reservation, deadline=None)
        for deadline in (None, False, self.now, float("inf")):
            self.db.execute("DELETE FROM integration_trains WHERE target_key=%s", (self.key,))
            port = train_reservation.Reservation(reservation,
                deadline=lambda plan, now: deadline, clock=lambda: self.now)
            result = self.passage(reservation=port).step(self.db, self.now)
            self.assertEqual(result["result"], "refused", result)
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)",
                                  "refs/heads/integration/"), "")
        self.assertEqual(self.api_calls, [])

    def test_ticket_for_another_contract_or_owner_cannot_resume(self):
        passage, ticket, plan = self.prepared()
        other = self.passage(owner="runner-b")
        self.assertEqual(other.step(self.fresh(), self.now + 1), {"result": "busy"})
        changed = dataclasses.replace(self.workflows[0], jobs=("other",))
        other = self.passage(workflows=(changed,))
        self.assertEqual(other.step(self.fresh(), self.now + 1), {"result": "busy"})
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)",
                                  "refs/heads/integration/"), "")

    def test_restart_retains_absolute_deadline_and_expiry_stops_live_lease(self):
        _, ticket, plan = self.prepared()
        self.decision_now = self.now + 30
        restarted = self.passage()
        result = restarted.step(self.fresh(), self.decision_now)
        self.assertEqual(result["result"], "ci", result)
        saved = self.ops.train_read(self.key)["reservation"]
        self.assertEqual(saved["deadline"], ticket.deadline)
        self.assertEqual(saved["epoch"], ticket.epoch)
        self.decision_now = ticket.deadline
        self.assertEqual(self.passage().step(self.fresh(), self.decision_now), {"result": "busy"})
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), plan["base"])
        self.assertIsNotNone(self.ops.land_lock(ticket.key, "next-writer", 90))

    def test_same_owner_new_epoch_cannot_replace_persisted_ticket(self):
        _, ticket, _ = self.prepared()
        newer = self.ops.land_lock(ticket.key, ticket.owner, 90)
        self.assertGreater(newer, ticket.epoch)
        self.assertEqual(self.passage().step(self.fresh(), self.now + 1), {"result": "busy"})
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)",
                                  "refs/heads/integration/"), "")


def exact_object(repository, commit, path):
    resolved = subprocess.check_output(["git", "-C", repository, "rev-parse", "--verify",
                                        commit + "^{commit}"], text=True).strip()
    if commit != resolved:
        raise ValueError("full exact supplier commit required")
    return subprocess.check_output(["git", "-C", repository, "show", commit + ":" + path])


def function(source, name, namespace, *, parent=None):
    nodes = ast.parse(source).body
    if parent:
        nodes = next(node for node in nodes if isinstance(node, ast.ClassDef)
                     and node.name == parent).body
    node = next(node for node in nodes if isinstance(node, ast.FunctionDef) and node.name == name)
    compiled = compile(ast.Module(body=[node], type_ignores=[]), "<exact supplier object>", "exec")
    namespace = dict(namespace)
    exec(compiled, namespace)
    return namespace[name]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--supplier", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--land-commit", required=True)
    parser.add_argument("--boundary", action="store_true",
                        help="also run the original six caller-boundary witnesses")
    args = parser.parse_args()
    packet_source = exact_object(args.supplier, args.commit, "src/ameesh/packet_reservation.py")
    lock_source = exact_object(args.supplier, args.commit, "src/ameesh/storage/postgres/work.py")
    key_source = exact_object(args.supplier, args.land_commit, "src/ameesh/land.py")
    original_key = land.lock_key
    lock = function(lock_source, "land_lock", vars(postgres_work), parent="WorkItems")
    key = function(key_source, "lock_key", vars(land))
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
        path = os.path.join(directory, "packet_reservation.py")
        with open(path, "wb") as output:
            output.write(packet_source)
        spec = importlib.util.spec_from_file_location("ameesh.packet_reservation", path)
        reservation = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = reservation
        spec.loader.exec_module(reservation)
        with mock.patch.object(land, "lock_key", key), \
                mock.patch.object(postgres_work.WorkItems, "land_lock", lock):
            result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(
                cls(name) for cls in ((ReservationConsumerTest, InstalledReservationTest)
                                     if args.boundary else (InstalledReservationTest,))
                for name in cls.__dict__ if name.startswith("test_")))
        sys.exit(not result.wasSuccessful())
