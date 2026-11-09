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
#: identité d'occurrence de la demande (#6487), posée avec la consigne
OCCURRENCE = "occ-1"


def _row(pending=PROMPT, *, occurrence=OCCURRENCE, lease_owner="r", status="queued"):
    return {"status": status, "pending_prompt": pending,
            "pending_prompt_id": occurrence if pending else None,
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
        key = repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT, OCCURRENCE)
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
                            repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT, OCCURRENCE))

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
                         repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT, OCCURRENCE))

    def test_a_turn_that_launched_is_settled_completed(self):
        w = self._worker()
        key = repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT, OCCURRENCE)

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
        key = repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT, OCCURRENCE)

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
        key = repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT, OCCURRENCE)
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
        key = repair_retry.runner_retry_key(PROJECT, "rlx", PROMPT, OCCURRENCE)

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
        occurrence = registry.get(self.db, "dev")["pending_prompt_id"]
        self.assertTrue(occurrence)  # #6487 : posée avec la consigne
        w = self._worker(self.epoch)
        spec = self._pick(w)
        self.assertEqual(spec["kind"], "prompt")
        key = repair_retry.runner_retry_key(PROJECT, "dev", "consigne du lot",
                                            occurrence)
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
        current = self.db.query(
            "SELECT current_prompt_id FROM agent_registry WHERE name='dev'")[0]
        self.assertEqual(str(current["current_prompt_id"]), str(occurrence))

    def test_two_successive_identical_instructions_are_two_entries(self):
        """Le scénario du B : le même texte redemandé après un tour achevé."""
        first = self._pick(self._worker(self.epoch))
        first_key = first["_retry_key"]
        self.assertTrue(repair_retry.observe_runner_retry_settled(
            self.db, key=first_key, outcome="completed"))
        # la consigne est redemandée, texte identique : demande neuve
        registry.set_pending_prompt(self.db, "dev", "consigne du lot")
        second = self._pick(self._worker(self.epoch))
        self.assertNotEqual(second["_retry_key"], first_key)
        self.assertEqual(
            repair_retry.from_record(storage.of(self.db).repair_signals.retry_get(
                first_key)).state, "completed")
        self.assertEqual(
            repair_retry.from_record(storage.of(self.db).repair_signals.retry_get(
                second["_retry_key"])).state, "engaged")

    def test_a_joined_instruction_keeps_the_same_entry(self):
        """B #204724 : la reprise joint une consigne et enrichit le texte de
        la même demande ; l'entrée engagée reste une seule."""
        first = self._pick(self._worker(self.epoch))
        first_key = first["_retry_key"]
        # une consigne arrive pendant le tour ; la restauration la joint au
        # tour inachevé en gardant l'occurrence de celui-ci
        registry.set_pending_prompt(self.db, "dev", "suite du lot")
        self.assertTrue(registry.restore_prompt(
            self.db, "dev", "runner", self.epoch))
        row = registry.get(self.db, "dev")
        self.assertEqual(row["pending_prompt"],
                         "consigne du lot\n\nsuite du lot")
        second = self._pick(self._worker(self.epoch))
        self.assertEqual(second["_retry_key"], first_key)
        self.assertEqual(
            repair_retry.from_record(storage.of(self.db).repair_signals.retry_get(
                first_key)).state, "engaged")


class RunnerRetryKeyTest(unittest.TestCase):
    """La clé est stable entre détenteurs et propre à l'occurrence."""

    def test_one_queued_instruction_has_one_key(self):
        self.assertEqual(
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT, OCCURRENCE),
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT, OCCURRENCE))
        self.assertRegex(
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT, OCCURRENCE),
            r"^[0-9a-f]{64}$")
        # l'occurrence porte seule l'identité : le texte peut être enrichi par
        # une reprise (brief de relève, consigne jointe) sans nouvelle demande
        self.assertEqual(
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT, OCCURRENCE),
            repair_retry.runner_retry_key(PROJECT, AGENT,
                                          "brief\n\n" + PROMPT, OCCURRENCE))
        # sans occurrence (ligne antérieure à 0562), le texte fait foi
        self.assertNotEqual(
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT),
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT + " bis"))
        self.assertNotEqual(
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT, OCCURRENCE),
            repair_retry.runner_retry_avoided_key(PROJECT, AGENT, PROMPT,
                                                  OCCURRENCE))

    def test_the_occurrence_alone_identifies_the_request(self):
        """B #204724 : texte enrichi, même occurrence = même demande."""
        self.assertEqual(
            repair_retry.runner_retry_key(PROJECT, AGENT, "continue", "occ-1"),
            repair_retry.runner_retry_key(PROJECT, AGENT,
                                          "continue\n\nsuite", "occ-1"))
        self.assertEqual(
            repair_retry.runner_retry_avoided_key(PROJECT, AGENT, "continue",
                                                  "occ-1"),
            repair_retry.runner_retry_avoided_key(PROJECT, AGENT,
                                                  "continue\n\nsuite", "occ-1"))
        # une clé d'occurrence ne rencontre pas la clé d'un texte, même si le
        # texte vaut l'identifiant
        self.assertNotEqual(
            repair_retry.runner_retry_key(PROJECT, AGENT, "occ-1", "occ-1"),
            repair_retry.runner_retry_key(PROJECT, AGENT, "occ-1"))

    def test_the_same_text_under_two_occurrences_is_two_requests(self):
        """Le B codex1 : deux demandes identiques ne partagent pas la clé."""
        self.assertNotEqual(
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT, "occ-1"),
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT, "occ-2"))
        # une ligne antérieure à 0562, sans occurrence, garde une clé calculable
        self.assertRegex(
            repair_retry.runner_retry_key(PROJECT, AGENT, PROMPT),
            r"^[0-9a-f]{64}$")

    def test_the_prompt_text_never_enters_the_journal(self):
        record = repair_retry.Retry(
            project=PROJECT, environment=ENVIRONMENT, origin="automatic",
            intent="turn.retry", cause="unknown", episode="agent:" + AGENT,
            idempotency_key=repair_retry.runner_retry_key(
                PROJECT, AGENT, PROMPT, OCCURRENCE),
            requested_at="2026-11-09T12:00:00Z", attempt=1)
        self.assertNotIn(PROMPT, repr(record.record()))


class PendingPromptOccurrenceTest(PgTestCase):
    """#6487 : l'occurrence est posée, transportée et rendue avec la consigne."""

    def setUp(self):
        super().setUp()
        suffix = self.id().rsplit(".", 1)[-1].replace("_", "-")[:40]
        self.agent = "occ-" + suffix
        registry.upsert(self.db, self.agent, harness="codex", host="source")
        lease = registry.claim(self.db, self.agent, "runner", ttl_seconds=300)
        self.epoch = lease["lease_epoch"]

    def prompt_id(self):
        return registry.get(self.db, self.agent)["pending_prompt_id"]

    def take(self):
        prompt = registry.take_pending_prompt(
            self.db, self.agent, "runner", self.epoch)
        row = registry.get(self.db, self.agent)
        return {"prompt": prompt, "id": row["current_prompt_id"]}

    def test_a_new_enqueue_renews_the_occurrence(self):
        registry.set_pending_prompt(self.db, self.agent, "continue")
        first = self.prompt_id()
        self.assertTrue(first)
        taken = self.take()
        self.assertEqual(taken["prompt"], "continue")
        self.assertEqual(str(taken["id"]), str(first))
        # même texte, nouvelle mise en attente : c'est une demande neuve
        registry.set_pending_prompt(self.db, self.agent, "continue")
        second = self.prompt_id()
        self.assertTrue(second)
        self.assertNotEqual(str(second), str(first))

    def test_a_restored_request_keeps_its_occurrence(self):
        registry.set_pending_prompt(self.db, self.agent, "continue")
        first = self.prompt_id()
        self.assertEqual(str(self.take()["id"]), str(first))
        self.assertTrue(registry.restore_prompt(
            self.db, self.agent, "runner", self.epoch))
        # la même demande, restaurée : même occurrence, pas une seconde
        self.assertEqual(str(self.prompt_id()), str(first))
        self.assertEqual(str(self.take()["id"]), str(first))


#: Écritures de la version N-1 (`d92f3465e`), copiées telles quelles : elles
#: ne connaissent ni `pending_prompt_id` ni `current_prompt_id` (#204724).
_OLD_SET_PENDING_PROMPT = """
    UPDATE agent_registry
       SET pending_prompt = %s,
           status = CASE WHEN %s::text IS NULL THEN status ELSE 'queued' END,
           updated_at = now()
     WHERE name = %s
"""

_OLD_TAKE_PENDING_PROMPT = """
    WITH verrou AS (
        SELECT name, status, handoff_fenced, operator_paused, pending_prompt,
               lease_owner, lease_epoch, lease_expires_at
          FROM agent_registry WHERE name = %s FOR UPDATE
    )
    UPDATE agent_registry AS r
       SET pending_prompt = NULL,
           current_prompt = verrou.pending_prompt,
           status = 'running',
           status_text = 'turn in progress',
           updated_at = now()
      FROM verrou
     WHERE r.name = verrou.name
       AND verrou.lease_owner = %s AND verrou.lease_epoch = %s
       AND verrou.pending_prompt IS NOT NULL
       AND NOT verrou.operator_paused
       AND NOT verrou.handoff_fenced
       AND ameesh_agent_claims_open(r.host, r.name)
       AND verrou.status <> 'stopped'
       AND verrou.lease_expires_at > clock_timestamp()
    RETURNING verrou.pending_prompt
"""

_OLD_RESTORE_PROMPT = """
    UPDATE agent_registry
       SET pending_prompt = CASE
               WHEN current_prompt IS NULL THEN pending_prompt
               WHEN pending_prompt IS NULL THEN current_prompt
               ELSE current_prompt || E'\\n\\n' || pending_prompt
           END,
           current_prompt = NULL,
           status = CASE WHEN operator_paused AND status = 'running'
                         THEN 'queued' ELSE status END,
           updated_at = now()
     WHERE name = %s AND lease_owner = %s AND lease_epoch = %s
       AND current_prompt IS NOT NULL
    RETURNING name
"""


#: `Handoffs.switch` de la version N-1 (`d92f3465e`), copiée telle quelle.
_OLD_HANDOFF_SWITCH = """
    UPDATE agent_registry SET host=%s,cwd=%s,lease_owner=NULL,lease_expires_at=NULL,
     lease_epoch=lease_epoch+1,session_id=NULL,session_account=NULL,session_work_item=NULL,
     session_reset_at=clock_timestamp(),placement_ok=NULL,placement_profile=NULL,
     pending_prompt=concat_ws(E'\\n\\n',%s::text,pending_prompt),
     status='queued',status_text='host handoff',updated_at=now() WHERE name=%s
"""

#: `Operations.resume` de la version N-1 (`d92f3465e`), copiée telle quelle.
_OLD_RESUME = """
    WITH verrou AS (
        SELECT name, status, session_id,
               (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL
                AND lease_expires_at > clock_timestamp()) AS live
          FROM agent_registry WHERE name = %s FOR UPDATE
    ), p AS (SELECT %s::boolean AS oubli, %s::text AS consigne)
    UPDATE agent_registry AS r
       SET session_id = CASE WHEN p.oubli THEN NULL ELSE r.session_id END,
           session_work_item = CASE WHEN p.oubli THEN NULL
                                    ELSE r.session_work_item END,
           election_brief = CASE WHEN p.oubli THEN NULL
                                 ELSE r.election_brief END,
           session_reset_at = CASE WHEN p.oubli THEN clock_timestamp()
                                   ELSE r.session_reset_at END,
           pending_prompt = CASE
               WHEN verrou.live THEN concat_ws(E'\\n\\n', r.pending_prompt,
                                               p.consigne)
               ELSE concat_ws(E'\\n\\n', p.consigne, r.current_prompt,
                              r.pending_prompt) END,
           current_prompt = CASE WHEN verrou.live THEN r.current_prompt
                                 ELSE NULL END,
           restart_brief = CASE WHEN verrou.live THEN r.restart_brief ELSE NULL END,
           restart_requested_at = CASE WHEN verrou.live THEN r.restart_requested_at
                                       ELSE NULL END,
           status = 'queued',
           status_text = %s::text,
           last_seen = now(),
           updated_at = now()
      FROM verrou, p
     WHERE r.name = verrou.name
       AND (NOT verrou.live
            OR (NOT p.oubli AND verrou.status NOT IN ('running', 'stopped')))
"""


class PreviousWriterOccurrenceTest(PgTestCase):
    """N-1 (#204724) : l'invariant d'occurrence tient sur les écritures de la
    version précédente, qui ignorent les deux colonnes."""

    ADDED = ("pending_prompt_id", "current_prompt_id")

    def setUp(self):
        super().setUp()
        suffix = self.id().rsplit(".", 1)[-1].replace("_", "-")[:40]
        self.agent = "n1-" + suffix
        registry.upsert(self.db, self.agent, harness="codex", host="source")
        lease = registry.claim(self.db, self.agent, "runner", ttl_seconds=300)
        self.epoch = lease["lease_epoch"]

    def old_enqueue(self, text):
        self.previous_write(_OLD_SET_PENDING_PROMPT,
                            (text, text, self.agent), added=self.ADDED)
        return registry.get(self.db, self.agent)

    def old_take(self):
        self.previous_write(_OLD_TAKE_PENDING_PROMPT,
                            (self.agent, "runner", self.epoch),
                            added=self.ADDED)
        return registry.get(self.db, self.agent)

    def old_restore(self):
        self.previous_write(_OLD_RESTORE_PROMPT,
                            (self.agent, "runner", self.epoch),
                            added=self.ADDED)
        return registry.get(self.db, self.agent)

    def old_handoff(self, brief):
        self.previous_write(_OLD_HANDOFF_SWITCH,
                            ("target", "work", brief, self.agent),
                            added=self.ADDED)
        return registry.get(self.db, self.agent)

    def old_resume(self, prompt):
        self.previous_write(_OLD_RESUME,
                            (self.agent, False, prompt,
                             "resumed by operator"),
                            added=self.ADDED)
        return registry.get(self.db, self.agent)

    def test_two_old_enqueues_are_two_occurrences(self):
        """Le témoin du B : set N-1, take N-1, set N-1, même texte."""
        first = self.old_enqueue("continue")
        self.assertTrue(first["pending_prompt_id"])
        taken = self.old_take()
        self.assertEqual(str(taken["current_prompt_id"]),
                         str(first["pending_prompt_id"]))
        second = self.old_enqueue("continue")
        self.assertTrue(second["pending_prompt_id"])
        self.assertNotEqual(str(second["pending_prompt_id"]),
                            str(first["pending_prompt_id"]))
        self.assertNotEqual(
            repair_retry.runner_retry_key(PROJECT, self.agent, "continue",
                                          first["pending_prompt_id"]),
            repair_retry.runner_retry_key(PROJECT, self.agent, "continue",
                                          second["pending_prompt_id"]))

    def test_an_old_restore_returns_the_occurrence(self):
        occurrence = self.old_enqueue("continue")["pending_prompt_id"]
        self.old_take()
        restored = self.old_restore()
        self.assertEqual(restored["pending_prompt"], "continue")
        self.assertEqual(str(restored["pending_prompt_id"]), str(occurrence))
        # le nouveau lecteur retrouve la même demande au tour suivant
        self.assertEqual(str(self.old_take()["current_prompt_id"]),
                         str(occurrence))

    def test_an_old_restore_with_a_joined_instruction_keeps_one_identity(self):
        """N-1 (#204724) : take, consigne posée, restore joint — le texte
        change mais la demande inachevée garde son identité."""
        occurrence = self.old_enqueue("continue")["pending_prompt_id"]
        self.old_take()
        self.old_enqueue("suite")
        restored = self.old_restore()
        self.assertEqual(restored["pending_prompt"], "continue\n\nsuite")
        self.assertEqual(str(restored["pending_prompt_id"]), str(occurrence))
        self.assertEqual(
            repair_retry.runner_retry_key(PROJECT, self.agent, "continue",
                                          occurrence),
            repair_retry.runner_retry_key(PROJECT, self.agent,
                                          "continue\n\nsuite", occurrence))


    def test_an_old_replacement_of_a_pending_request_renews_the_occurrence(self):
        """Le scénario du B #204799 : le remplacement N-1 d'une attente non
        nulle est une demande neuve, pas un enrichissement."""
        self.old_enqueue("continue")
        self.old_take()
        self.old_enqueue("suite")
        restored = self.old_restore()
        self.assertEqual(restored["pending_prompt"], "continue\n\nsuite")
        previous = str(restored["pending_prompt_id"])
        # l'ancien `set_pending_prompt` remplace le texte sans toucher aux ids
        replaced = self.old_enqueue("nouvelle demande")
        self.assertEqual(replaced["pending_prompt"], "nouvelle demande")
        self.assertTrue(replaced["pending_prompt_id"])
        self.assertNotEqual(str(replaced["pending_prompt_id"]), previous)
        self.assertNotEqual(
            repair_retry.runner_retry_key(PROJECT, self.agent,
                                          "continue\n\nsuite", previous),
            repair_retry.runner_retry_key(PROJECT, self.agent,
                                          "nouvelle demande",
                                          replaced["pending_prompt_id"]))

    def test_an_old_replacement_of_an_unconsumed_request_renews_it(self):
        first = self.old_enqueue("continue")
        second = self.old_enqueue("autre consigne")
        self.assertEqual(second["pending_prompt"], "autre consigne")
        self.assertNotEqual(str(second["pending_prompt_id"]),
                            str(first["pending_prompt_id"]))

    def test_two_old_enqueues_of_the_same_text_are_two_occurrences(self):
        """B codex1 #204850 (1) : deux `set_pending_prompt` N-1 successifs du
        même texte, sans consommation, sont deux demandes — le déclencheur ne
        peut pas se fier à un texte inchangé."""
        first = self.old_enqueue("continue")
        second = self.old_enqueue("continue")
        self.assertEqual(second["pending_prompt"], "continue")
        self.assertTrue(second["pending_prompt_id"])
        self.assertNotEqual(str(second["pending_prompt_id"]),
                            str(first["pending_prompt_id"]))
        self.assertNotEqual(
            repair_retry.runner_retry_key(PROJECT, self.agent, "continue",
                                          first["pending_prompt_id"]),
            repair_retry.runner_retry_key(PROJECT, self.agent, "continue",
                                          second["pending_prompt_id"]))

    def test_an_old_replacement_that_ends_like_the_pending_is_new(self):
        """B codex1 #204850 (2) : « ne pas continue » se termine par l'attente
        existante ; ce suffixe ne prouve rien, c'est une demande indépendante."""
        first = self.old_enqueue("continue")
        replaced = self.old_enqueue("ne pas continue")
        self.assertEqual(replaced["pending_prompt"], "ne pas continue")
        self.assertTrue(replaced["pending_prompt_id"])
        self.assertNotEqual(str(replaced["pending_prompt_id"]),
                            str(first["pending_prompt_id"]))
        self.assertNotEqual(
            repair_retry.runner_retry_key(PROJECT, self.agent, "continue",
                                          first["pending_prompt_id"]),
            repair_retry.runner_retry_key(PROJECT, self.agent,
                                          "ne pas continue",
                                          replaced["pending_prompt_id"]))

    def test_an_old_handoff_brief_keeps_the_occurrence(self):
        """N-1 (#204850) : le brief de relève s'ajoute à l'attente ; l'hôte,
        le bail et la session déplacés disent la reprise, pas une demande
        neuve — l'identité se garde sans lire le texte."""
        first = self.old_enqueue("continue")["pending_prompt_id"]
        moved = self.old_handoff("now on target")
        self.assertEqual(moved["pending_prompt"], "now on target\n\ncontinue")
        self.assertEqual(str(moved["pending_prompt_id"]), str(first))

    def test_an_old_resume_under_a_live_lease_opens_a_new_demand(self):
        """N-1 (#204850) : `resume` sous bail vivant ajoute une consigne à la
        file — c'est une demande neuve, comme le nouveau code."""
        first = self.old_enqueue("continue")["pending_prompt_id"]
        resumed = self.old_resume("suite")
        self.assertEqual(resumed["pending_prompt"], "continue\n\nsuite")
        self.assertNotEqual(str(resumed["pending_prompt_id"]), str(first))

    def test_an_old_resume_outside_a_lease_keeps_the_reprise(self):
        """N-1 (#204850) : hors bail, la consigne reçue rejoint la demande
        reprise ; celle-ci garde son occurrence."""
        first = self.old_enqueue("continue")["pending_prompt_id"]
        self.db.execute("UPDATE agent_registry SET lease_expires_at = "
                        "clock_timestamp() - interval '1 minute' WHERE name = %s",
                        (self.agent,))
        resumed = self.old_resume("suite")
        self.assertEqual(resumed["pending_prompt"], "suite\n\ncontinue")
        self.assertEqual(str(resumed["pending_prompt_id"]), str(first))

    def test_two_old_resumes_with_the_same_status_keep_the_reprise(self):
        """B/P2 impl-11 et cx1 (#204850) : deux `resume` N-1 hors bail avec le
        même `status_text` sont deux reprises de la même demande ; le second
        ne retombe pas dans la branche `set_pending_prompt`. `last_seen` est
        rafraîchi à chaque appel, `status_text` ne l'est pas."""
        first = self.old_enqueue("continue")["pending_prompt_id"]
        self.db.execute("UPDATE agent_registry SET lease_expires_at = "
                        "clock_timestamp() - interval '1 minute' WHERE name = %s",
                        (self.agent,))
        one = self.old_resume("suite")
        two = self.old_resume("encore")
        self.assertEqual(one["pending_prompt"], "suite\n\ncontinue")
        self.assertEqual(two["pending_prompt"], "encore\n\nsuite\n\ncontinue")
        self.assertEqual(str(one["pending_prompt_id"]), str(first))
        self.assertEqual(str(two["pending_prompt_id"]), str(first))


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
        self.occurrence = "occ-" + suffix[:40]

    def key(self):
        return repair_retry.runner_retry_key(self.project, self.agent,
                                             self.prompt, self.occurrence)

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
            self.project, self.agent, self.prompt, self.occurrence)
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

    def test_two_identical_requests_are_two_entries(self):
        """Le B codex1 : même texte, deux occurrences = deux demandes."""
        first = repair_retry.runner_retry_key(
            self.project, self.agent, self.prompt, "occ-a")
        second = repair_retry.runner_retry_key(
            self.project, self.agent, self.prompt, "occ-b")
        self.assertNotEqual(first, second)
        for key in (first, second):
            self.assertTrue(self.requested(key)["created"])
            self.assertTrue(repair_retry.observe_runner_retry_engaged(
                self.db, key=key))
            self.assertTrue(repair_retry.observe_runner_retry_settled(
                self.db, key=key, outcome="completed"))
            self.assertEqual(self.record(key).state, "completed")

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
