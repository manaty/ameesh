# SPDX-License-Identifier: AGPL-3.0-only
"""Train wake-up routing and commit visibility, with no live CI or train."""
import json
import threading
import time
import types
import unittest
from unittest import mock

from ameesh import ci, storage, train_events, train_runner, train_store, work
from ameesh.runner import Runner
from .support import PgTestCase


class OwnNamespaceListener:
    """Filtre d'écoute des tests : le canal est commun, le schéma ne l'est pas.

    `ameesh_train` est un canal NOTIFY de la base entière : un test d'une autre
    classe (autre schéma) peut livrer son réveil pendant une attente. La
    production filtre déjà par namespace (`train_events.receive`) ; les tests
    qui inspectent l'enveloppe brute font de même ici, sinon un message
    étranger satisfait (ou casse) leurs attentes négatives (#3933).
    """

    def __init__(self, listener, namespace):
        self.listener = listener
        self.namespace = namespace

    def own(self, item):
        if not isinstance(item, dict):
            return True
        if "event" in item:  # panne de l'écouteur : jamais avalée
            return True
        try:
            signal = json.loads(item.get("payload"))
        except (TypeError, ValueError, RecursionError, UnicodeError):
            return False
        return isinstance(signal, dict) and signal.get("namespace") == self.namespace

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            item = self.listener.wait(max(0.0, deadline - time.monotonic()))
            if item is None or self.own(item):
                return item
            if time.monotonic() >= deadline:
                return None

    def close(self):
        self.listener.close()


class TrainEventRoutingTest(unittest.TestCase):
    def service(self):
        passage = types.SimpleNamespace(project=types.SimpleNamespace(
            name="p", target="refs/remotes/origin/main"))
        service = train_runner.Service(types.SimpleNamespace(schema="test_mesh"),
            [passage], stop=threading.Event(), report=mock.Mock())
        service.pending.clear()
        return service

    def notification(self, **fields):
        signal = dict(schema=train_events.SCHEMA, namespace="test_mesh",
                      kind="ci-terminal", project="p", target="main")
        signal.update(fields)
        return dict(channel=train_events.CHANNEL, payload=json.dumps(signal))

    def test_exact_events_coalesce_without_any_proof_or_storage_lookup(self):
        service = self.service()
        with mock.patch.object(storage, "of", side_effect=AssertionError("proof lookup")):
            for kind in train_events.KINDS:
                self.assertTrue(train_events.receive(service, self.notification(kind=kind)))
        self.assertEqual(service.pending, {0})
        service.stop.set()
        self.assertFalse(train_events.receive(service, self.notification()))

    def test_malformed_unrelated_and_authority_bearing_payloads_do_not_schedule(self):
        service = self.service()
        cases = [None, {}, dict(channel="agent_mail", payload="{}")]
        cases += [dict(channel=train_events.CHANNEL, payload=value) for value in
                  (None, b"{}", "{", "null", "[]", "true", "[" * 2000,
                   "x" * (train_events.MAX_BYTES + 1), '"\ud800"')]
        cases += [self.notification(**fields) for fields in (
            dict(namespace="another_mesh"), dict(namespace=None),
            dict(schema="future"), dict(kind="ci-pending"),
            dict(project="another"), dict(project=["p"]),
            dict(target="release/x"), dict(target="origin/main"),
            dict(target="refs/heads/main"), dict(target=" main"),
            dict(target="main..x"), dict(target="main\n"),
            dict(verdict="ok"), dict(head="a" * 40), dict(authorized=True))]
        for item in cases:
            with self.subTest(item=repr(item)[:120]):
                self.assertFalse(train_events.receive(service, item))
        self.assertEqual(service.pending, set())

    def test_producer_refuses_aliases_and_invalid_subject_before_sql(self):
        db = types.SimpleNamespace(cfg=types.SimpleNamespace(schema="test_mesh"))
        with mock.patch.object(storage, "of", side_effect=AssertionError("unexpected SQL")):
            for fields in (dict(target="origin/main"), dict(kind="ci-pending"),
                           dict(project=""), dict(target="main/"),
                           dict(target="x" * train_events.MAX_BYTES)):
                args = dict(kind="verdict", project="p", target="main")
                args.update(fields)
                with self.subTest(fields=repr(fields)[:100]), self.assertRaises(ValueError):
                    train_events.emit(db, **args)

    @mock.patch("ameesh.migrations.require_compatible_writer")
    def test_runner_dispatch_only_schedules_train_without_waking_workers(self, admission):
        runner = Runner.__new__(Runner)
        runner.db = object()
        runner.train_service = self.service()
        runner.wake_all = mock.Mock()
        with mock.patch.object(storage, "of", side_effect=AssertionError("SQL in dispatch")):
            runner.dispatch(self.notification())
        self.assertEqual(runner.train_service.pending, {0})
        runner.wake_all.set.assert_not_called()
        runner.train_service = None
        runner.dispatch(self.notification())
        runner.wake_all.set.assert_not_called()
        self.assertEqual(admission.call_args_list, [mock.call(runner.db), mock.call(runner.db)])

    def test_listener_subscribes_before_recovery_and_timeouts_never_wake_train(self):
        runner = Runner.__new__(Runner)
        runner.train_service = mock.Mock()
        runner.cfg = types.SimpleNamespace(connect_timeout=1)
        runner.db, runner.wake_all = object(), mock.Mock()
        runner.stop = threading.Event()
        runner.db_retry_max, runner.poll = 1, 1
        listener = mock.Mock()
        calls = []

        def subscribe(channels):
            self.assertIn(train_events.CHANNEL, channels)
            calls.append("subscribed")
            return listener

        def wait(**_):
            self.assertEqual(calls, ["subscribed", "recovery"])
            runner.stop.set()
            return None

        runner.train_service.wake.side_effect = lambda: calls.append("recovery")
        listener.wait.side_effect = wait
        with mock.patch.object(storage, "of") as st:
            st.return_value.wakeups.subscribe.side_effect = subscribe
            runner._listen_loop()
        runner.train_service.wake.assert_called_once_with()
        runner.train_service.event.assert_not_called()
        listener.close.assert_called_once_with()


class TrainEventTransactionTest(PgTestCase):
    #: schéma d'un test d'une autre classe, sur le même canal (database-wide)
    FOREIGN_NAMESPACE = "t_other_3933"

    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE ci_runs, merge_locks RESTART IDENTITY CASCADE")

    def listener(self):
        listener = storage.of(self.db).wakeups.subscribe([train_events.CHANNEL])
        self.assertIsNotNone(listener, "LISTEN is required for train event qualification")
        self.addCleanup(listener.close)
        return OwnNamespaceListener(listener, self.cfg.schema)

    def emit(self, db, **fields):
        args = dict(kind="verdict", project="event-test", target="main")
        args.update(fields)
        train_events.emit(db, **args)

    def foreign_hint(self):
        return json.dumps(dict(schema=train_events.SCHEMA,
                               namespace=self.FOREIGN_NAMESPACE, kind="verdict",
                               project="event-test", target="main"))

    def test_other_namespace_hint_during_wait_is_ignored(self):
        listener = self.listener()
        # Le canal est commun à toute la base : une transaction d'un autre
        # schéma peut livrer son réveil pendant l'attente. La file du même
        # producteur garde l'ordre, donc l'écouteur pré-#3933 rend le message
        # étranger ici.
        self.db.execute("SELECT pg_notify(%s, %s)",
                        (train_events.CHANNEL, self.foreign_hint()))
        self.emit(self.db)
        item = listener.wait(3)
        self.assertIsNotNone(item)
        self.assertEqual(json.loads(item["payload"])["namespace"], self.cfg.schema,
                         "a hint from another namespace satisfied this test's wait")
        self.assertIsNone(listener.wait(0.1),
                          "a hint from another namespace failed a negative wait")

    def test_committed_event_schedules_fresh_passage_reading_the_committed_state(self):
        listener = self.listener()
        project = types.SimpleNamespace(name="event-test", target="main")
        ledger = train_store.Ledger(self.db, project, "main", "events")
        epoch = ledger.acquire(None)
        self.addCleanup(ledger.release, epoch)
        state = ledger.save(dict(phase="ci"), 0, epoch)
        service = train_runner.Service(self.cfg, (), stop=threading.Event(), report=mock.Mock())
        seen = []

        def passage(db, now):
            self.assertIsNot(db, self.db)
            seen.append(storage.of(db).work.train_read(ledger.key))
            service.stop.set()
            return {"result": "fixture"}, None

        service.passages = (types.SimpleNamespace(project=project, scheduled_step=passage),)
        with self.db.transaction() as tx:
            stored = storage.of(tx).work.train_save(ledger.key, dict(phase="ready"),
                revision=state["revision"], owner=ledger.owner, epoch=epoch)
            self.assertIsNotNone(stored)
            self.emit(tx, kind="ci-terminal")
            self.assertIsNone(listener.wait(0.1), "hint escaped before producer commit")
        item = listener.wait(3)
        self.assertIsNotNone(item, "committed train notification missing")
        self.assertTrue(train_events.receive(service, item))
        self.assertEqual(seen, [], "notification dispatch ran the passage itself")
        service.run()
        self.assertEqual(seen, [stored])
        service.report.assert_not_called()

    def test_rollback_discards_hint_and_multiple_events_coalesce_after_commit(self):
        listener = self.listener()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.db.transaction() as tx:
                self.emit(tx)
                raise RuntimeError("rollback")
        self.assertIsNone(listener.wait(0.1))
        # Identical hints in one transaction coalesce in PostgreSQL. Only the
        # current SQL state will matter when the service reconstructs Passage.
        with self.db.transaction() as tx:
            self.emit(tx)
            self.emit(tx)
        item = listener.wait(3)
        self.assertIsNotNone(item)
        self.assertEqual(json.loads(item["payload"])["kind"], "verdict")
        self.assertIsNone(listener.wait(0.1))

    def test_same_database_different_namespace_does_not_wake_service(self):
        listener = self.listener()
        self.emit(self.db)
        item = listener.wait(3)
        self.assertIsNotNone(item)
        service = train_runner.Service(types.SimpleNamespace(schema=self.schema + "_other"),
            [types.SimpleNamespace(project=types.SimpleNamespace(name="event-test", target="main"))],
            stop=threading.Event(), report=mock.Mock())
        service.pending.clear()
        self.assertFalse(train_events.receive(service, item))
        self.assertEqual(service.pending, set())

    def test_verdict_producer_commits_hint_with_milestone_and_rollback_loses_both(self):
        item = work.add(self.db, title="event fixture", app="event-test", source="fixture",
                        estimate=5, estimate_source="fixture", actor="fixture")
        work.set_branch(self.db, item["id"], "lot/events", "origin/main", actor="fixture")
        # #2962 : un verdict complet doit nommer la tête gelée courante (ou se
        # résoudre) ; le gel de fixture est posé au stockage, ce témoin-ci
        # n'exerçant pas la porte.
        storage.of(self.db).work.add_milestone(
            item["id"], "frozen", sha="a" * 40, actor="fixture", verdict=None, note="")
        listener = self.listener()
        with self.db.transaction() as tx:
            work.milestone(tx, item["id"], "verdict", sha="a" * 40,
                           actor="reviewer", verdict="ok")
            self.assertIsNone(listener.wait(0.1))
        signal = json.loads(listener.wait(3)["payload"])
        self.assertEqual((signal["project"], signal["target"], signal["kind"]),
                         ("event-test", "main", "verdict"))
        self.assertEqual(len(work.milestones(self.db, item["id"])), 3)  # request + freeze + verdict
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.db.transaction() as tx:
                work.milestone(tx, item["id"], "verdict", sha="a" * 40,
                               actor="other", verdict="blocked")
                raise RuntimeError("rollback")
        self.assertIsNone(listener.wait(0.1))
        self.assertEqual(len(work.milestones(self.db, item["id"])), 3)
        # A milestone which is not a verdict supplies no train event.
        work.milestone(self.db, item["id"], "frozen", sha="a" * 40, actor="fixture")
        self.assertIsNone(listener.wait(0.1))

    def packet(self):
        project = types.SimpleNamespace(name="event-test", target="develop")
        ledger = train_store.Ledger(self.db, project, "develop", "events")
        epoch = ledger.acquire(None)
        self.addCleanup(ledger.release, epoch)
        # Explicit routing fixture, not an executable packet or CI proof.
        plan = dict(source="github.com/o/r", target="develop", head="a" * 40,
                    branch="integration/train-fixture")
        ledger.save(dict(phase="ci", plan=plan), 0, epoch)
        return plan

    def run_row(self, number, **fields):
        args = dict(head_sha="a" * 40, branch="integration/train-fixture", event="push",
                    base="main", work_item_id=None, url=None)
        args.update(fields)
        return storage.of(self.db).ci.open_run("o/r", number, **args)

    def test_ci_producer_routes_all_terminal_statuses_without_recipient_or_default_base(self):
        self.packet()
        listener = self.listener()
        for number, status in enumerate(("success", "failure", "cancelled"), 1):
            with self.subTest(status=status):
                row = self.run_row(number)
                watcher = ci.Watcher(self.cfg, api=lambda _: dict(status="completed", conclusion=status))
                result = watcher._conclude(self.db, "o/r", row)
                self.assertEqual((result[0]["status"], result[0]["to"]), (status, None))
                signal = json.loads(listener.wait(3)["payload"])
                self.assertEqual((signal["project"], signal["target"], signal["kind"]),
                                 ("event-test", "develop", "ci-terminal"))
                self.assertEqual(storage.of(self.db).ci.run("o/r", number)["status"], status)
                self.assertFalse(storage.of(self.db).ci.conclude("o/r", number, status))
                self.assertIsNone(listener.wait(0.1), "duplicate conclusion emitted another hint")

    def test_ci_update_and_hint_share_transaction_and_other_heads_branches_sources_are_silent(self):
        self.packet()
        listener = self.listener()
        self.run_row(1)
        with self.assertRaisesRegex(RuntimeError, "transport failed"), \
                mock.patch.object(train_events, "emit", side_effect=RuntimeError("transport failed")):
            storage.of(self.db).ci.conclude("o/r", 1, "success")
        self.assertEqual(storage.of(self.db).ci.run("o/r", 1)["status"], "running")
        self.assertIsNone(listener.wait(0.1))
        with self.db.transaction() as tx:
            self.assertTrue(storage.of(tx).ci.conclude("o/r", 1, "success"))
            self.assertIsNone(listener.wait(0.1))
        self.assertEqual(json.loads(listener.wait(3)["payload"])["target"], "develop")
        for number, fields in enumerate((dict(head_sha="b" * 40), dict(branch="main")), 2):
            self.run_row(number, **fields)
            self.assertTrue(storage.of(self.db).ci.conclude("o/r", number, "success"))
            self.assertIsNone(listener.wait(0.1))
        other = dict(head_sha="a" * 40, branch="integration/train-fixture", event="push",
                     base="main", work_item_id=None, url=None)
        storage.of(self.db).ci.open_run("another/r", 4, **other)
        self.assertTrue(storage.of(self.db).ci.conclude("another/r", 4, "success"))
        self.assertIsNone(listener.wait(0.1))
