# SPDX-License-Identifier: AGPL-3.0-only
"""Runner lifecycle and event scheduling, without a live train or CI."""
import threading
import types
import unittest
from unittest import mock

from ameesh import train, train_runner
from ameesh.runner import Runner


class TrainServiceTest(unittest.TestCase):
    def service(self, passage, **kw):
        return train_runner.Service(object(), [passage], stop=threading.Event(),
                                    report=mock.Mock(), **kw)

    def test_passes_use_distinct_scoped_connections_closed_on_success_and_error(self):
        passage = mock.Mock()
        passage.scheduled_step.side_effect = [({"result": "ci"}, 200), RuntimeError("closed")]
        service = self.service(passage)
        first, second = mock.Mock(), mock.Mock()
        with mock.patch("ameesh.scoped_db.ScopedDb", side_effect=[first, second]):
            self.assertEqual(service.pass_once(0, 100), 200)
            self.assertIsNone(service.pass_once(0, 101))
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()
        self.assertEqual(passage.scheduled_step.call_args_list,
                         [mock.call(first, 100), mock.call(second, 101)])
        service.report.assert_called_once()

    def test_schedule_and_engine_share_the_same_compiled_object(self):
        passage = train_runner.Passage.__new__(train_runner.Passage)
        passage.policy = train.Policy(enabled=True)
        runtime = mock.Mock()
        runtime.policy.schedule.next_packet.return_value = 1234
        passage.runtime = mock.Mock(return_value=runtime)
        passage._step = mock.Mock(return_value={"result": "ci"})
        db = object()
        self.assertEqual(passage.scheduled_step(db, 100), ({"result": "ci"}, 1234))
        passage.runtime.assert_called_once_with(db)
        runtime.policy.schedule.next_packet.assert_called_once_with(100)
        runtime.policy.schedule.next_cutoff.assert_not_called()
        passage._step.assert_called_once_with(runtime, 100)
        for deadline in (True, 100, 99, float("nan"), float("inf")):
            runtime.policy.schedule.next_packet.return_value = deadline
            with self.assertRaises(train.TrainError):
                passage.scheduled_step(db, 100)
        self.assertEqual(passage._step.call_count, 1)

    def test_stop_prevents_new_connection_and_passage(self):
        passage = mock.Mock()
        service = self.service(passage)
        service.stop.set()
        with mock.patch("ameesh.scoped_db.ScopedDb") as connect:
            self.assertIsNone(service.pass_once(0, 100))
        connect.assert_not_called()
        passage.scheduled_step.assert_not_called()

    def test_cutoff_wait_uses_compiled_deadline_and_event_wakes_without_polling(self):
        service = self.service(mock.Mock(), clock=lambda: 100)
        seen = []

        def passage(index, now):
            seen.append((index, now))
            if len(seen) == 2:
                service.stop.set()
            return 9876

        def wait(timeout):
            self.assertEqual(timeout, 9776)
            service.wake()  # notification, not a periodic CI probe

        with mock.patch.object(service, "pass_once", side_effect=passage), \
             mock.patch.object(service.condition, "wait", side_effect=wait) as waiting:
            service.run()
        self.assertEqual(seen, [(0, 100), (0, 100)])
        waiting.assert_called_once()

    def test_failure_waits_for_an_event_instead_of_retrying(self):
        service = self.service(mock.Mock(), clock=lambda: 100)

        def wait(timeout):
            self.assertIsNone(timeout)
            service.stop.set()

        with mock.patch.object(service, "pass_once", return_value=None) as passage, \
             mock.patch.object(service.condition, "wait", side_effect=wait):
            service.run()
        passage.assert_called_once_with(0, 100)

    def test_event_filters_project_target_and_terminal_kind_and_coalesces(self):
        passage = types.SimpleNamespace(project=types.SimpleNamespace(name="p", target="main"))
        service = self.service(passage)
        service.pending.clear()
        for fields in (dict(kind="ci-pending", project="p", target="main"),
                       dict(kind="ci-terminal", project="other", target="main"),
                       dict(kind="verdict", project="p", target="release/x")):
            self.assertFalse(service.event(**fields))
        self.assertEqual(service.pending, set())
        for kind in ("ci-terminal", "verdict", "fire"):
            self.assertTrue(service.event(kind=kind, project="p", target="main"))
        self.assertEqual(service.pending, {0})
        service.stop.set()
        self.assertFalse(service.event(kind="fire", project="p", target="main"))

    def test_event_arriving_during_passage_is_not_lost(self):
        passage = types.SimpleNamespace(project=types.SimpleNamespace(name="p", target="main"))
        service = self.service(passage, clock=lambda: 100)
        seen = []

        def run(index, now):
            seen.append(index)
            if len(seen) == 1:
                self.assertTrue(service.event(kind="ci-terminal", project="p", target="main"))
            else:
                service.stop.set()
            return 500

        with mock.patch.object(service, "pass_once", side_effect=run), \
             mock.patch.object(service.condition, "wait", side_effect=AssertionError("lost event")):
            service.run()
        self.assertEqual(seen, [0, 0])

    def test_shutdown_interrupts_the_calendar_wait(self):
        service = self.service(mock.Mock())
        # No work: the service waits indefinitely for an explicit event.
        service.pending.clear()
        service.start()
        service.stop.set()
        service.shutdown()
        self.assertFalse(service.thread.is_alive())

    def test_maintenance_has_its_own_fresh_connection_and_never_runs_passage(self):
        passage = mock.Mock()
        passage.maintain.return_value = 300
        service = self.service(passage)
        first, second = mock.Mock(), mock.Mock()
        with mock.patch("ameesh.scoped_db.ScopedDb", side_effect=[first, second]):
            self.assertEqual(service.maintain_once(0, 100), 300)
            passage.maintain.side_effect = RuntimeError("lost epoch")
            self.assertIsNone(service.maintain_once(0, 200))
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()
        passage.scheduled_step.assert_not_called()
        service.report.assert_called_once()

    def test_renewal_timer_does_not_become_a_ci_poll(self):
        passage = types.SimpleNamespace(maintenance_at=200)
        service = self.service(passage, clock=lambda: 100)
        service.pending.clear()
        service.deadlines[0] = 9876
        service.maintenance[0] = 100

        def wait(timeout):
            self.assertEqual(timeout, 200)
            service.stop.set()

        with mock.patch.object(service, "pass_once", side_effect=AssertionError("CI poll")), \
             mock.patch.object(service, "maintain_once", return_value=300) as renew, \
             mock.patch.object(service.condition, "wait", side_effect=wait):
            service.run()
        renew.assert_called_once_with(0, 100)

    def test_event_and_due_maintenance_share_one_passage_instead_of_double_renewal(self):
        passage = types.SimpleNamespace(maintenance_at=300)
        service = self.service(passage, clock=lambda: 100)
        service.maintenance[0] = 100

        def run(index, now):
            service.stop.set()
            return 9876

        with mock.patch.object(service, "pass_once", side_effect=run) as run_pass, \
             mock.patch.object(service, "maintain_once", side_effect=AssertionError("duplicate")):
            service.run()
        run_pass.assert_called_once_with(0, 100)
        self.assertEqual(service.maintenance, {0: 300})

    def test_runner_guards_and_single_start(self):
        runner = Runner.__new__(Runner)
        passage = types.SimpleNamespace(policy=train.Policy(enabled=True), reservation=object())
        runner.train_passages, runner.train_service = [passage], None
        runner.cfg, runner.stop = object(), threading.Event()
        runner.once = runner.dry_run = runner.mediated = False
        with mock.patch.object(train_runner, "Service") as service:
            for guard in ("once", "dry_run", "mediated"):
                setattr(runner, guard, True)
                runner.start_train_service()
                setattr(runner, guard, False)
            runner.train_passages = []
            runner.start_train_service()
            runner.train_passages = [types.SimpleNamespace(policy=train.Policy())]
            runner.start_train_service()
            runner.train_passages = [types.SimpleNamespace(policy=train.Policy(enabled=True))]
            runner.start_train_service()
            service.assert_not_called()
            runner.train_passages = [passage]
            runner.start_train_service()
            runner.start_train_service()
            service.assert_called_once()
            service.return_value.start.assert_called_once_with()
