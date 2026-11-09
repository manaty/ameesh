# SPDX-License-Identifier: AGPL-3.0-only
"""#6487 : journal A11 des relances automatiques du runner (U14 suite).

Témoins demandés par le lot : relance consommée une fois, tour perdu par
perte de bail (unknown), deux détenteurs, échec d'écriture du journal sans
effet sur le tour, comptage par origine.  L'observation est best effort :
aucun de ces chemins ne change l'admission, le bail ni l'ordre des tours.
"""
from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from ameesh import lot_workdir, registry, repair_retry, runner, storage
from ameesh.runner import AgentWorker

from .support import PgTestCase
from .test_6313_repair_report import capability, run


PROJECT = "ameesh"
ENVIRONMENT = "staging"
AGENT = "dev"
PROMPT = "reprends le lot #12"


def _row(pending=PROMPT, *, lease_owner="r", status="queued"):
    return {"status": status, "pending_prompt": pending,
            "lease_owner": lease_owner, "lease_epoch": 1,
            "team": PROJECT, "chantier": "", "canon_governed": True}


class RunnerRetryHooksTest(unittest.TestCase):
    """Les points d'appel du runner, sans base : ordre et best effort."""

    def _worker(self, pending=PROMPT):
        w = AgentWorker.__new__(AgentWorker)
        threading.Thread.__init__(w, name="rlx", daemon=True)  # jamais démarré
        w.db, w.epoch = object(), 1
        w.name = "rlx"
        w.cfg = SimpleNamespace(project=PROJECT)
        w.agent = _row(pending)
        w.lease_lost = threading.Event()
        w.runner = SimpleNamespace(runner_id="r", idle_nudge=1200.0,
                                   gate_holds=lambda: False,
                                   host_pressure=lambda: {})
        w.rate_limit, w.rate_limit_streak = None, 0
        w.rate_limit_since, w.rate_limit_retry_at = 0.0, 0.0
        w.nudged, w.last_activity = False, time.monotonic()
        w._session_en_attente, w.suite, w._reprise_attach = None, None, False
        return w

    def _pick(self, w, *, taken=PROMPT, row=None):
        oui, non = mock.Mock(return_value=True), mock.Mock(return_value=False)
        rows = w.agent if row is None else row
        if not isinstance(rows, list):
            rows = [rows]
        if len(rows) == 1:
            registry_get = mock.patch.object(runner.registry, "get",
                                             return_value=rows[0])
        else:
            registry_get = mock.patch.object(runner.registry, "get",
                                             side_effect=rows)
        with mock.patch.multiple(
                w, _marque_base=non, maybe_relocate=non, budget_ok=oui,
                _held_by_pressure=non, _host_pressure_release=mock.Mock(),
                host_ready=oui, workdir_ready=oui, auth_ready=oui,
                credits_ready=oui, turn_lot=mock.Mock(return_value=None),
                _lot_workdir_blocked=non,
                _scheduled_wake_spec=mock.Mock(return_value=None),
                _lot_wake_spec=mock.Mock(return_value=None)), \
                registry_get, \
                mock.patch.object(runner.registry, "take_pending_prompt",
                                  return_value=taken), \
                mock.patch.object(lot_workdir, "deliverable_unread",
                                  side_effect=lambda *a, **k: (
                                      ([], False) if k.get("troncature") else [])), \
                mock.patch.object(runner.account_turn, "continuity",
                                  return_value=True):
            return w._pick()

    def test_a_queued_instruction_is_requested_then_engaged_once(self):
        w = self._worker()
        key = repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT)
        with mock.patch.object(
                repair_retry, "observe_runner_retry_requested") as requested, \
                mock.patch.object(repair_retry, "observe_runner_retry_engaged") as engaged:
            spec = self._pick(w)
        self.assertEqual(spec["kind"], "prompt")
        self.assertEqual(spec["_retry_key"], key)
        requested.assert_called_once()
        kwargs = requested.call_args.kwargs
        self.assertEqual(kwargs["key"], key)
        self.assertEqual(kwargs["project"], PROJECT)
        self.assertEqual(kwargs["environment"], ENVIRONMENT)
        self.assertEqual(kwargs["agent"], "rlx")
        self.assertEqual(kwargs["key"], engaged.call_args.kwargs["key"])
        self.assertIn("turn.retry", repair_retry.INTENTS)

    def test_a_second_pick_without_a_queued_state_does_not_replay(self):
        w = self._worker()
        with mock.patch.object(
                repair_retry, "observe_runner_retry_requested") as requested, \
                mock.patch.object(repair_retry, "observe_runner_retry_engaged") as engaged:
            self.assertIsNotNone(self._pick(w))
            # plus rien en attente : le tour suivant ne rejoue pas l'entrée
            w.agent = _row(None, status="idle")
            self.assertIsNone(self._pick(w, taken=None))
        self.assertEqual(requested.call_count, 1)
        self.assertEqual(engaged.call_count, 1)

    def test_a_lost_consumption_is_avoided_by_idempotence(self):
        w = self._worker()
        # un autre détenteur a consommé la consigne entre la lecture et la prise
        after = _row(None, lease_owner="other", status="running")
        with mock.patch.object(
                repair_retry, "observe_runner_retry_requested") as requested, \
                mock.patch.object(repair_retry, "observe_runner_retry_engaged") as engaged, \
                mock.patch.object(repair_retry, "observe_runner_retry_avoided") as avoided:
            self.assertIsNone(self._pick(w, taken=None,
                                         row=[_row(PROMPT), after]))
        requested.assert_called_once()
        engaged.assert_not_called()
        self.assertEqual(avoided.call_args.kwargs["cause"], "other_holder")
        self.assertEqual(avoided.call_args.kwargs["agent"], "rlx")
        # aucune tentative consommée par l'évitée : clé distincte et dédiée
        self.assertNotEqual(avoided.call_args.kwargs["key"],
                            repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT))

    def test_a_refused_admission_is_avoided_with_its_own_cause(self):
        w = self._worker()
        # la consigne est toujours en attente, le bail est le nôtre, mais la
        # prise est refusée (garde) : ce n'est pas un autre détenteur.
        after = _row(PROMPT, lease_owner="r", status="queued")
        with mock.patch.object(
                repair_retry, "observe_runner_retry_requested"), \
                mock.patch.object(
                    repair_retry, "observe_runner_retry_avoided") as avoided:
            self.assertIsNone(self._pick(w, taken=None, row=after))
        self.assertEqual(avoided.call_args.kwargs["cause"], "admission_refused")

    def test_a_journal_failure_never_changes_the_turn(self):
        w = self._worker()
        with mock.patch.object(
                repair_retry, "observe_runner_retry_requested",
                side_effect=RuntimeError("journal down")), \
                mock.patch.object(
                    repair_retry, "observe_runner_retry_engaged",
                    side_effect=RuntimeError("journal down")):
            spec = self._pick(w)
        self.assertEqual(spec["kind"], "prompt")
        self.assertEqual(spec["prompt"], PROMPT)
        # la clé reste connue : l'issue pourra être tentative plus tard
        self.assertEqual(spec["_retry_key"],
                         repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT))

    def test_a_turn_that_launched_is_settled_completed(self):
        w = self._worker()
        key = repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT)

        def run_turn_spec(spec):
            w._turn_launched = True
            return True

        w._run_turn_or_defer = mock.Mock(side_effect=run_turn_spec)
        w._purge_turn_context = mock.Mock()
        with mock.patch.object(repair_retry, "observe_runner_retry_settled") as settled:
            self.assertTrue(AgentWorker.run_turn.__wrapped__(
                w, {"kind": "prompt", "_retry_key": key}))
        self.assertEqual(settled.call_args.kwargs, {
            "key": key, "outcome": "completed", "log": runner.log})

    def test_a_lost_lease_settles_unknown(self):
        w = self._worker()
        key = repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT)

        def lost(spec):
            w._turn_launched = True
            w.lease_lost.set()
            return False

        w._run_turn_or_defer = mock.Mock(side_effect=lost)
        w._purge_turn_context = mock.Mock()
        with mock.patch.object(repair_retry, "observe_runner_retry_settled") as settled:
            self.assertFalse(AgentWorker.run_turn.__wrapped__(
                w, {"kind": "prompt", "_retry_key": key}))
        self.assertEqual(settled.call_args.kwargs["outcome"], "unknown")

    def test_a_turn_that_never_launched_leaves_the_entry_engaged(self):
        w = self._worker()
        key = repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT)
        w._run_turn_or_defer = mock.Mock(return_value=None)  # tour différé
        w._purge_turn_context = mock.Mock()
        with mock.patch.object(repair_retry, "observe_runner_retry_settled") as settled:
            self.assertIsNone(AgentWorker.run_turn.__wrapped__(
                w, {"kind": "prompt", "_retry_key": key}))
        settled.assert_not_called()

    def test_a_turn_without_a_queued_instruction_is_not_journaled(self):
        w = self._worker()
        w._run_turn_or_defer = mock.Mock(return_value=True)
        w._purge_turn_context = mock.Mock()
        with mock.patch.object(repair_retry, "observe_runner_retry_settled") as settled:
            self.assertTrue(AgentWorker.run_turn.__wrapped__(w, {"kind": "mail"}))
        settled.assert_not_called()

    def test_a_settlement_failure_never_changes_the_turn(self):
        w = self._worker()
        key = repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT)

        def run_turn_spec(spec):
            w._turn_launched = True
            return True

        w._run_turn_or_defer = mock.Mock(side_effect=run_turn_spec)
        w._purge_turn_context = mock.Mock()
        with mock.patch.object(repair_retry, "observe_runner_retry_settled",
                               side_effect=RuntimeError("journal down")):
            self.assertTrue(AgentWorker.run_turn.__wrapped__(
                w, {"kind": "prompt", "_retry_key": key}))


class RunnerRetryPickIntegrationTest(PgTestCase):
    """Le point d'appel réel, sur une base : la prise engage le journal."""

    def setUp(self):
        super().setUp()
        registry.upsert(self.db, "dev", harness="codex", host="source")
        lease = registry.claim(self.db, "dev", "runner", ttl_seconds=300)
        self.epoch = lease["lease_epoch"]
        registry.set_pending_prompt(self.db, "dev", "consigne du lot")

    def _worker(self, epoch):
        w = AgentWorker.__new__(AgentWorker)
        threading.Thread.__init__(w, name="rlx", daemon=True)
        w.db, w.epoch = self.db, epoch
        w.name = "dev"
        w.cfg = SimpleNamespace(project=PROJECT)
        w.agent = registry.get(self.db, "dev")
        w.lease_lost = threading.Event()
        w.runner = SimpleNamespace(runner_id="runner", idle_nudge=1200.0,
                                   gate_holds=lambda: False,
                                   host_pressure=lambda: {})
        w.rate_limit, w.rate_limit_streak = None, 0
        w.rate_limit_since, w.rate_limit_retry_at = 0.0, 0.0
        w.nudged, w.last_activity = False, time.monotonic()
        w._session_en_attente, w.suite, w._reprise_attach = None, None, False
        return w

    def _pick(self, w):
        oui, non = mock.Mock(return_value=True), mock.Mock(return_value=False)
        with mock.patch.multiple(
                w, _marque_base=non, maybe_relocate=non, budget_ok=oui,
                _held_by_pressure=non, _host_pressure_release=mock.Mock(),
                host_ready=oui, workdir_ready=oui, auth_ready=oui,
                credits_ready=oui, turn_lot=mock.Mock(return_value=None),
                _lot_workdir_blocked=non,
                _scheduled_wake_spec=mock.Mock(return_value=None),
                _lot_wake_spec=mock.Mock(return_value=None)), \
                mock.patch.object(lot_workdir, "deliverable_unread",
                                  side_effect=lambda *a, **k: (
                                      ([], False) if k.get("troncature") else [])), \
                mock.patch.object(runner.account_turn, "continuity",
                                  return_value=True):
            return w._pick()

    def test_the_real_pick_engages_the_journal_entry(self):
        w = self._worker(self.epoch)
        spec = self._pick(w)
        self.assertEqual(spec["kind"], "prompt")
        key = repair_retry.runner_retry_key(PROJECT, "dev", "consigne du lot")
        self.assertEqual(spec["_retry_key"], key)
        record = repair_retry.from_record(
            storage.of(self.db).repair_signals.retry_get(key))
        self.assertEqual(record.state, "engaged")
        self.assertEqual(record.origin, "automatic")
        self.assertEqual(record.intent, "turn.retry")
        self.assertEqual(record.episode, "agent:dev")
        row = registry.get(self.db, "dev")
        self.assertIsNone(row["pending_prompt"])
        self.assertEqual(row["status"], "running")


class RunnerRetryKeyTest(unittest.TestCase):
    """La clé est stable entre détenteurs et propre à la consigne."""

    def test_one_queued_instruction_has_one_key(self):
        self.assertEqual(
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT),
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT))
        self.assertRegex(repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT),
                         r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT),
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT + " bis"))
        self.assertNotEqual(
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT),
            repair_retry.runner_retry_avoided_key(PROJECT, AGENT, PROMPT))

    def test_the_prompt_text_never_enters_the_journal(self):
        record = repair_retry.Retry(
            project=PROJECT, environment=ENVIRONMENT, origin="automatic",
            intent="turn.retry", cause="unknown", episode="agent:" + AGENT,
            idempotency_key=repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT),
            requested_at="2026-11-09T12:00:00Z", attempt=1)
        self.assertNotIn(PROMPT, repr(record.record()))


class RunnerRetryJournalStorageTest(PgTestCase):
    """Le journal réel : deux détenteurs, une tentative, issues terminales.

    Le banc partage les tables du journal entre les témoins (elles ne sont
    pas dans le TRUNCATE de `PgTestCase`) : chaque témoin prend donc son
    projet et son agent, comme les clés distinctes de #6475.
    """

    def setUp(self):
        super().setUp()
        suffix = self.id().rsplit(".", 1)[-1].replace("_", "-")[:40]
        self.project = "ameesh-" + suffix
        self.agent = "dev-" + suffix[:40]
        self.prompt = PROMPT + ":" + suffix

    def key(self):
        return repair_retry.runner_retry_key(self.project, self.agent,
                                             self.prompt)

    def requested(self, key, **kw):
        values = dict(project=self.project, environment=ENVIRONMENT,
                      agent=self.agent, cause="unknown")
        values.update(kw)
        return repair_retry.observe_runner_retry_requested(
            self.db, key=key, **values)

    def record(self, key):
        return repair_retry.from_record(
            storage.of(self.db).repair_signals.retry_get(key))

    def test_two_holders_share_one_attempt_and_the_loser_is_avoided(self):
        key = self.key()
        first = self.requested(key)
        second = self.requested(key)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["attempt"], 1)  # pas de seconde tentative
        # un seul détenteur gagne l'engagement, l'autre journalise évitée
        self.assertTrue(repair_retry.observe_runner_retry_engaged(self.db, key=key))
        self.assertFalse(repair_retry.observe_runner_retry_engaged(self.db, key=key))
        avoid = repair_retry.runner_retry_avoided_key(
            self.project, self.agent, self.prompt)
        self.assertTrue(repair_retry.observe_runner_retry_avoided(
            self.db, key=avoid, project=self.project, environment=ENVIRONMENT,
            agent=self.agent, cause="other_holder"))
        self.assertFalse(repair_retry.observe_runner_retry_avoided(
            self.db, key=avoid, project=self.project, environment=ENVIRONMENT,
            agent=self.agent, cause="other_holder"))
        self.assertEqual(self.record(key).state, "engaged")
        self.assertEqual(self.record(avoid).state, "avoided")
        self.assertEqual(self.record(avoid).attempt, 0)
        attempts = storage.of(self.db).repair_signals.retry_attempts(
            self.project, ENVIRONMENT, "agent:" + self.agent, "unknown")
        self.assertEqual(attempts, 1)

    def test_completed_is_terminal_and_resets_the_consecutive_counter(self):
        key = self.key()
        self.requested(key)
        self.assertTrue(repair_retry.observe_runner_retry_engaged(self.db, key=key))
        self.assertTrue(repair_retry.observe_runner_retry_settled(
            self.db, key=key, outcome="completed"))
        self.assertFalse(repair_retry.observe_runner_retry_settled(
            self.db, key=key, outcome="completed"))
        self.assertFalse(repair_retry.observe_runner_retry_settled(
            self.db, key=key, outcome="unknown"))
        self.assertEqual(self.record(key).state, "completed")
        self.assertEqual(storage.of(self.db).repair_signals.retry_attempts(
            self.project, ENVIRONMENT, "agent:" + self.agent, "unknown"), 0)

    def test_a_lost_lease_leaves_an_unknown_outcome(self):
        key = self.key()
        self.requested(key)
        self.assertTrue(repair_retry.observe_runner_retry_engaged(self.db, key=key))
        self.assertTrue(repair_retry.observe_runner_retry_settled(
            self.db, key=key, outcome="unknown"))
        record = self.record(key)
        self.assertEqual(record.state, "unknown")
        self.assertEqual(record.origin, "automatic")
        self.assertEqual(record.intent, "turn.retry")
        self.assertEqual(record.episode, "agent:" + self.agent)

    def test_the_report_counts_runner_relaunches_next_to_manual_resumes(self):
        key = self.key()
        self.requested(key)
        repair_retry.observe_runner_retry_engaged(self.db, key=key)
        repair_retry.observe_runner_retry_settled(self.db, key=key,
                                                  outcome="completed")
        repair_retry.observe_manual_resume(
            self.db, project=self.project, environment=ENVIRONMENT,
            agent="other", actor="", origin="operator", invocation="inv-1")
        summary = storage.of(self.db).repair_signals.retry_summary(
            project=self.project)
        self.assertEqual(summary["by_origin"]["automatic"]["completed"], 1)
        self.assertEqual(summary["by_origin"]["operator"]["completed"], 1)
        self.assertEqual(summary["totals"]["requested"], 2)


class RunnerRetryReportTest(unittest.TestCase):
    """Le rapport U1 nomme la relance du runner et ne la déclare plus absente."""

    def test_the_a11_limit_is_lifted_for_the_queued_runner_relaunch(self):
        a11 = capability(run(), "A11")
        limits = " ".join(a11["limits"])
        self.assertIn("consigne en attente", limits)
        self.assertIn("origine automatique", limits)
        self.assertNotIn("ne sont pas encore raccordées et ne sont pas comptées",
                         limits)
