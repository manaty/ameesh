# SPDX-License-Identifier: AGPL-3.0-only
"""#1922 — native Process registry: revision/stage and idempotent receipts.

Targeted proofs on a real PostgreSQL, with both drivers (the bench
selects psycopg or psql via `AMEESH_DRIVER`):

* nominal advance, replay after a fresh connection, intent conflict,
  concurrency of the same key and of two commands at the same version;
* authority: unknown/stopped actor, lease of another runner, stale epoch,
  expired lease, race with a revocation (the registry row is held
  `FOR SHARE` for the whole transaction);
* holds `blocked`/`waiting_human` (the latter requires a native
  decision request open, and an answer before release): entry and exit
  go through the reserved verifier `hold` (slice C: `verify_hold`), an
  advance from a hold is refused (`held`), terminal never reopened
  (even by adoption), bounded repair loop;
* explicit revision transfer, never automatic, without regression or
  native core change, without verifier injection by the caller
  (the public API only reads the installed registry);
* rollback without a half-event (trigger that breaks the UPDATE), existing
  history intact, lots without workflow unchanged; `work.move`/`work.close`/
  `work.close_merged` refuse a pinned lot (the registry is the only
  writer);
* constraints of additive migration 0452: complete receipt required `IS TRUE`
  (any missing key or nested `null` is refused), partial uniqueness
  (lot, key);
* reading of the guard by the storage interface (`work.get`), including
  on a pre-0452 database (registry columns absent): no column
  probe, no raw SELECT (B5).

Organizations and agents are fictitious (`acme`). No activation:
`INSTALLED_VERIFIERS` is empty by default, the tests inject their
verifiers through the documented seam.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import glob
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import dataclass
from unittest import mock

from ameesh import work_lifecycle as lifecycle
from ameesh import process_registry as pr
from ameesh import storage
from ameesh import work
from ameesh.db import DbError

from .support import REPO, PgTestCase

try:  # slice A (#1918), not merged yet when these tests were written
    from ameesh import process_workflow as pw
except ImportError:  # pragma: no cover - depends on the state of main
    pw = None

try:  # slice C (#1924), not merged yet when these tests were written
    from ameesh import process_verifiers as pv
except ImportError:  # pragma: no cover - depends on the state of main
    pv = None

ACME_REF = "acme/process-workflow@0f1e2d3c"
ACME_VERSION = 3
ACME_ACTOR = "acme-dev1"
ACME_RUNNER = "acme-runner-1"
ACME_EPOCH = 4


@dataclass(frozen=True)
class _Stage:
    status: str
    core: str


@dataclass(frozen=True)
class _Transition:
    source: str
    target: str
    guard: str


ACME_STAGES = (
    _Stage("draft", "intake"),
    _Stage("implementation", "build"),
    _Stage("review", "qa"),
    _Stage("staging", "merged"),
    _Stage("done", "promoted"),
    _Stage("stopped", "closed"),
)
ACME_TRANSITIONS = (
    _Transition("draft", "implementation", "planning"),
    _Transition("implementation", "review", "review_requested"),
    _Transition("review", "staging", "qa_started"),
    _Transition("review", "implementation", "qa_repair"),
    _Transition("staging", "done", "work_completed"),
    _Transition("draft", "stopped", "stop_work"),
)
ACME_GUARDS = tuple(t.guard for t in ACME_TRANSITIONS)


class _Spec:
    """Minimal artifact that satisfies the published `WorkflowSpec` protocol."""

    def __init__(self, *, ref: str = ACME_REF, version: int = ACME_VERSION,
                 initial: str = "draft", stages=ACME_STAGES,
                 transitions=ACME_TRANSITIONS) -> None:
        self.workflow_ref = ref
        self.workflow_version = version
        self.initial = initial
        self._stages = tuple(stages)
        self._transitions = tuple(transitions)

    def statuses(self):
        return tuple(stage.status for stage in self._stages)

    def core_of(self, status):
        for stage in self._stages:
            if stage.status == status:
                return stage.core
        raise KeyError(status)

    def transitions_from(self, source):
        return tuple(t for t in self._transitions if t.source == source)

    def stage(self, status):
        for stage in self._stages:
            if stage.status == status:
                return stage
        raise KeyError(status)


def _ok(context=None):
    return pr.Verification(ok=True, reason="proven")


class RegistryTest(PgTestCase):
    """Base: a bound runner agent, a pinned lot, one verifier per guard."""

    def setUp(self) -> None:
        super().setUp()
        self.spec = _Spec()
        self.calls: list[str] = []
        self.verifiers = {guard: self._recording(guard) for guard in ACME_GUARDS}

        def transfer(context):
            self.calls.append(pr.TRANSFER_VERIFIER)
            return pr.Verification(ok=True, reason="transfer re-read")

        self.verifiers[pr.TRANSFER_VERIFIER] = transfer

        def hold(context):
            self.calls.append(pr.HOLD_VERIFIER)
            return pr.Verification(ok=True, reason="hold re-read")

        self.verifiers[pr.HOLD_VERIFIER] = hold
        self.agent(ACME_ACTOR, lease_owner=ACME_RUNNER, epoch=ACME_EPOCH)

    # -- factories ---------------------------------------------------------

    def agent(self, name: str, *, status: str = "idle", lease_owner: str | None = None,
              epoch: int = 0, live: bool = True) -> None:
        self.db.execute(
            "INSERT INTO agent_registry (name, status, lease_owner, lease_epoch,"
            "                            lease_expires_at)"
            " VALUES (%s, %s, %s, %s,"
            "         CASE WHEN %s THEN now() + interval '1 hour'"
            "              ELSE now() - interval '1 hour' END)",
            (name, status, lease_owner, epoch, live))

    def lot(self, *, state: str = "intake", ref: str | None = None,
            stage: str | None = None, version: int = 0, loops: int = 0,
            title: str = "acme : un lot") -> int:
        row = self.db.query(
            "INSERT INTO work_items (title, state, app, workflow_ref, workflow_stage,"
            "                        workflow_version, loops)"
            " VALUES (%s, %s, 'acme', %s, %s, %s, %s) RETURNING id",
            (title, state, ref, stage, version, loops))
        return int(row[0]["id"])

    def pinned(self, *, stage: str = "draft", state: str = "intake",
               ref: str | None = None, version: int = ACME_VERSION,
               loops: int = 0) -> int:
        return self.lot(state=state, ref=ref or self.spec.workflow_ref,
                        stage=stage, version=version, loops=loops)

    def _recording(self, guard: str):
        def verifier(context):
            self.calls.append(guard)
            return _ok(context)
        return verifier

    def authority(self, *, actor: str = ACME_ACTOR, source: str = "runner",
                  epoch: int = ACME_EPOCH, runner_id: str = ACME_RUNNER):
        return pr.Authority(actor=actor, source=source, epoch=epoch,
                            runner_id=runner_id)

    def command(self, lot_id: int, *, action: str = "advance", frm: str = "draft",
                to: str = "implementation", guard: str = "planning",
                hold: str = "", version: int = 0, command_id: str | None = None,
                ref: str | None = None, revision: int = ACME_VERSION,
                authority=None, facts=None):
        return pr.Command(
            work_item_id=lot_id,
            command_id=command_id or str(uuid.uuid4()),
            expected_version=version,
            authority=authority or self.authority(),
            workflow_ref=ref or self.spec.workflow_ref,
            workflow_version=revision,
            action=action, from_stage=frm, to_stage=to, guard=guard, hold=hold,
            evidence=dict(facts or {}))

    def apply(self, command, *, spec=None, verifiers=None):
        # `_apply`: injection reserved for the bench; the public API only takes
        # the installed verifiers (`install_verifier`).
        return pr._apply(self.db, command, spec=spec or self.spec,
                         verifiers=self.verifiers if verifiers is None else verifiers)

    @contextlib.contextmanager
    def refusal(self, code: str):
        with self.assertRaises(pr.RegistryRefusal) as ctx:
            yield
        self.assertEqual(ctx.exception.code, code, str(ctx.exception))

    def events(self, lot_id: int) -> list[dict]:
        return self.db.query(
            "SELECT id, state, note, actor, command_id, workflow_stage,"
            "       workflow_version, evidence"
            "  FROM work_item_events WHERE work_item_id = %s ORDER BY id", (lot_id,))

    def row(self, lot_id: int) -> dict:
        return self.db.query("SELECT * FROM work_items WHERE id = %s", (lot_id,))[0]

    def journal_version(self, lot_id: int) -> int:
        return int(self.db.query(
            "SELECT coalesce(max(id), 0) AS v FROM work_item_events"
            " WHERE work_item_id = %s", (lot_id,))[0]["v"])

    def request_open_for(self, lot_id: int) -> int:
        """L233: an open native decision request for this lot."""
        row = self.db.query(
            "INSERT INTO agent_mailbox (sender, recipient, body, kind, payload,"
            "                           work_item_id)"
            " VALUES ('acme-orch', 'human:*', 'which option?', 'request',"
            "         '{\"decision\": {\"v\": 1, \"state\": \"pending\"}}'::jsonb, %s)"
            " RETURNING id", (str(lot_id),))
        return int(row[0]["id"])

    def milestones(self, lot_id: int) -> list[dict]:
        return self.db.query(
            "SELECT kind, sha, actor, note FROM work_item_milestones"
            " WHERE work_item_id = %s ORDER BY id", (lot_id,))

    def closure_milestones(self, lot_id: int) -> list[dict]:
        """Milestones set by a closure (the `requested` milestone comes with the lot)."""
        return [m for m in self.milestones(lot_id) if m["kind"] != "requested"]

    def dependent(self, blocker: int, *, state: str = "blocked",
                  assignee: str | None = "acme-dev2") -> int:
        """An explicit prerequisite of `blocker`, with a registered agent."""
        row = self.db.query(
            "INSERT INTO work_items (title, state, assignee, app)"
            " VALUES ('acme : dependent', %s, %s, 'acme') RETURNING id",
            (state, assignee))
        lot = int(row[0]["id"])
        self.db.execute(
            "INSERT INTO work_item_dependencies (work_item_id, blocker_id, created_by)"
            " VALUES (%s, %s, %s)", (lot, int(blocker), ACME_ACTOR))
        return lot

    def mail_to(self, recipient: str) -> list[dict]:
        return self.db.query(
            "SELECT id, body, payload FROM agent_mailbox"
            " WHERE recipient = %s AND kind = 'event' ORDER BY id", (recipient,))

    # -- nominal advance ---------------------------------------------------

    def test_nominal_advance_writes_core_stage_and_receipt(self):
        lot = self.pinned()
        cmd = self.command(lot)
        receipt = self.apply(cmd)
        self.assertTrue(receipt.accepted)
        self.assertFalse(receipt.replayed)
        self.assertEqual(receipt.state, "build")
        self.assertEqual(receipt.workflow_stage, "implementation")
        self.assertEqual(receipt.version, receipt.event_id)
        row = self.row(lot)
        self.assertEqual(row["state"], "build")
        self.assertEqual(row["workflow_stage"], "implementation")
        self.assertEqual(row["workflow_version"], ACME_VERSION)
        self.assertIsNotNone(row["started_at"])
        events = self.events(lot)
        self.assertEqual(len(events), 1)
        self.assertEqual(str(events[0]["command_id"]), cmd.command_id)
        evidence = events[0]["evidence"]
        self.assertEqual(evidence["schema"], pr.RECEIPT_SCHEMA)
        self.assertEqual(evidence["intent"]["sha256"], pr.intent_sha256(cmd))
        self.assertEqual(evidence["command"]["guard"], "planning")
        self.assertEqual(evidence["authority"]["epoch"], ACME_EPOCH)
        self.assertTrue(evidence["verification"]["required"])
        self.assertEqual(evidence["verification"]["verifier"], "planning")
        self.assertEqual(evidence["facts"], {})
        self.assertEqual(self.calls, ["planning"])

    def test_replay_same_intent_finds_the_receipt(self):
        lot = self.pinned()
        cmd = self.command(lot, facts={"ticket": "ACME-7"})
        first = self.apply(cmd)
        fresh = self.connect()                      # fresh connection
        try:
            again = pr._apply(fresh, cmd, spec=self.spec,
                              verifiers=self.verifiers)
        finally:
            fresh.close()
        self.assertTrue(again.replayed)
        self.assertEqual(again.event_id, first.event_id)
        self.assertEqual(len(self.events(lot)), 1)
        self.assertEqual(self.calls, ["planning"])   # the verifier did not replay

    def test_same_key_other_intent_conflict(self):
        lot = self.pinned()
        command_id = str(uuid.uuid4())
        self.apply(self.command(lot, command_id=command_id, facts={"ticket": "ACME-1"}))
        same_key = self.command(lot, command_id=command_id, facts={"ticket": "ACME-2"})
        with self.refusal("intent-conflict"):
            self.apply(same_key)
        changed = self.command(lot, command_id=command_id, to="stopped",
                               guard="stop_work", facts={"ticket": "ACME-1"})
        with self.refusal("intent-conflict"):
            self.apply(changed)
        self.assertEqual(len(self.events(lot)), 1)

    def test_optimistic_version_follows_the_journal(self):
        lot = self.pinned()
        first = self.apply(self.command(lot))
        self.assertEqual(first.version, self.journal_version(lot))
        stale = self.command(lot, frm="implementation", to="review",
                             guard="review_requested", version=first.version - 1)
        with self.refusal("version-conflict"):
            self.apply(stale)
        following = self.command(lot, frm="implementation", to="review",
                                 guard="review_requested", version=first.version)
        self.assertEqual(self.apply(following).state, "qa")

    def test_legacy_write_invalidates_the_version(self):
        lot = self.pinned()
        first = self.apply(self.command(lot))
        # `work.move` now refuses a pinned lot (review B3): the bench writes
        # the legacy journal line directly — it is the journal, not the
        # receipt counter, that is authoritative for the expected version.
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'blocked', 'legacy', %s)", (lot, ACME_ACTOR))
        self.assertGreater(self.journal_version(lot), first.version)
        stale = self.command(lot, frm="implementation", to="review",
                             guard="review_requested", version=first.version)
        with self.refusal("version-conflict"):
            self.apply(stale)

    # -- authority ---------------------------------------------------------

    def test_unknown_actor_refused(self):
        lot = self.pinned()
        cmd = self.command(lot, authority=pr.Authority(
            actor="acme-ghost", source="runner", epoch=1, runner_id=ACME_RUNNER))
        with self.refusal("authority-unknown-actor"):
            self.apply(cmd)
        self.assertEqual(self.row(lot)["state"], "intake")
        self.assertEqual(self.events(lot), [])

    def test_stopped_actor_refuses_even_a_replay(self):
        lot = self.pinned()
        cmd = self.command(lot)
        first = self.apply(cmd)
        self.db.execute("UPDATE agent_registry SET status = 'stopped' WHERE name = %s",
                        (ACME_ACTOR,))
        with self.refusal("authority-revoked"):
            self.apply(cmd)
        # the receipt exists, but a revoked actor does not get it back
        self.assertEqual(len(self.events(lot)), 1)
        self.assertEqual(first.event_id, self.events(lot)[0]["id"])

    def test_dead_actor_refused(self):
        lot = self.pinned()
        self.db.execute("UPDATE agent_registry SET status = 'dead' WHERE name = %s",
                        (ACME_ACTOR,))
        with self.refusal("authority-revoked"):
            self.apply(self.command(lot))
        self.assertEqual(self.events(lot), [])

    def test_stale_epoch_refuses_even_a_replay(self):
        lot = self.pinned()
        cmd = self.command(lot)
        self.apply(cmd)
        self.db.execute("UPDATE agent_registry SET lease_epoch = lease_epoch + 1"
                        " WHERE name = %s", (ACME_ACTOR,))
        with self.refusal("authority-epoch"):
            self.apply(cmd)

    def test_lease_of_another_runner_refused(self):
        lot = self.pinned()
        cmd = self.command(lot, authority=pr.Authority(
            actor=ACME_ACTOR, source="runner", epoch=ACME_EPOCH,
            runner_id="acme-runner-2"))
        with self.refusal("authority-lease"):
            self.apply(cmd)

    def test_expired_lease_refused(self):
        lot = self.pinned()
        self.db.execute("UPDATE agent_registry SET lease_expires_at = now() - interval '1 s'"
                        " WHERE name = %s", (ACME_ACTOR,))
        with self.refusal("authority-expired"):
            self.apply(self.command(lot))

    def test_session_source_without_lease(self):
        lot = self.pinned()
        self.agent("acme-dev2")           # neither lease nor epoch
        cmd = self.command(lot, authority=pr.Authority(actor="acme-dev2",
                                                       source="session", epoch=0))
        self.assertEqual(self.apply(cmd).state, "build")
        bad = self.command(lot, version=self.journal_version(lot),
                           authority=pr.Authority(actor="acme-dev2", source="session",
                                                  epoch=7))
        with self.refusal("authority-epoch"):
            self.apply(bad)

    def test_race_with_a_revocation(self):
        """The registry row holds FOR SHARE as long as the transaction lives."""
        lot = self.pinned()
        entered, release = threading.Event(), threading.Event()

        def blocking(context):
            entered.set()
            release.wait(10)
            return _ok(context)

        outcome: list = []

        def run():
            db = self.connect()
            try:
                outcome.append(pr._apply(
                    db, self.command(lot), spec=self.spec,
                    verifiers={"planning": blocking}))
            except Exception as exc:                     # pragma: no cover - diagnostic
                outcome.append(exc)
            finally:
                db.close()

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(10), "the verifier did not start")
        try:
            with self.assertRaises(DbError):
                with self.db.transaction() as tx:
                    tx.execute("SET LOCAL lock_timeout = '300ms'")
                    tx.execute("UPDATE agent_registry SET lease_epoch = lease_epoch + 1"
                               " WHERE name = %s", (ACME_ACTOR,))
        finally:
            release.set()
            thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], pr.Receipt)
        self.assertEqual(outcome[0].state, "build")

    # -- guards ------------------------------------------------------------

    def test_advance_without_installed_verifier(self):
        lot = self.pinned()
        with self.refusal("verifier-uninstalled"):
            self.apply(self.command(lot), verifiers={})
        self.assertEqual(self.events(lot), [])

    def test_verifier_that_refuses(self):
        lot = self.pinned()

        def refuses(context):
            return pr.Verification(ok=False, reason="missing proof")

        with self.refusal("verifier-refused"):
            self.apply(self.command(lot), verifiers={"planning": refuses})
        self.assertEqual(self.events(lot), [])

    def test_client_boolean_refused(self):
        lot = self.pinned()
        with self.refusal("verifier-invalid"):
            self.apply(self.command(lot), verifiers={"planning": lambda context: True})
        self.assertEqual(self.events(lot), [])

    def test_undeclared_edge(self):
        lot = self.pinned()
        with self.refusal("edge-undeclared"):
            self.apply(self.command(lot, to="review", guard="qa_started"))
        with self.refusal("edge-undeclared"):
            self.apply(self.command(lot, to="implementation", guard="qa_repair"))
        # #2086 N2: the direct stop is declared under `stop_work`, not under
        # `planning` — the pair exists, the (to, guard) edge does not.
        with self.refusal("edge-undeclared"):
            self.apply(self.command(lot, to="stopped", guard="planning"))
        self.assertEqual(self.events(lot), [])

    def test_different_current_stage(self):
        lot = self.pinned(stage="review", state="qa")
        with self.refusal("stage-mismatch"):
            self.apply(self.command(lot, frm="draft", to="implementation",
                                    guard="planning"))
        self.assertEqual(self.events(lot), [])

    def test_terminal_not_reopened(self):
        lot = self.lot(state="promoted", ref=self.spec.workflow_ref,
                       stage="done", version=ACME_VERSION)
        with self.refusal("terminal-reopened"):
            self.apply(self.command(lot, frm="done", to="staging",
                                    guard="qa_started", version=0))
        with self.refusal("terminal-reopened"):
            self.apply(self.command(lot, action="hold", hold="blocked",
                                    frm="", to="", guard="", version=0))
        self.assertEqual(self.events(lot), [])

    def test_bounded_repair_loop(self):
        lot = self.pinned(stage="review", state="qa", loops=0)
        first = self.apply(self.command(lot, frm="review", to="implementation",
                                        guard="qa_repair"))
        self.assertEqual(first.state, "build")
        self.assertEqual(self.row(lot)["loops"], 1)
        forward = self.apply(self.command(lot, frm="implementation", to="review",
                                          guard="review_requested", version=first.version))
        self.assertEqual(forward.state, "qa")
        self.assertEqual(self.row(lot)["loops"], 1)
        second = self.apply(self.command(lot, frm="review", to="implementation",
                                         guard="qa_repair", version=forward.version))
        self.assertEqual(self.row(lot)["loops"], 2)
        self.assertEqual(second.state, "build")
        forward = self.apply(self.command(lot, frm="implementation", to="review",
                                          guard="review_requested", version=second.version))
        exhausted = self.command(lot, frm="review", to="implementation",
                                 guard="qa_repair", version=forward.version)
        with self.refusal("repair-exhausted"):
            self.apply(exhausted)
        self.assertEqual(len(self.events(lot)), 4)

    # -- holds -------------------------------------------------------------

    def test_hold_and_release_keep_the_stage(self):
        lot = self.pinned(stage="implementation", state="build")
        held = self.apply(self.command(lot, action="hold", hold="blocked",
                                       frm="", to="", guard=""))
        self.assertEqual(held.state, "blocked")
        self.assertEqual(held.workflow_stage, "implementation")
        self.assertEqual(self.row(lot)["workflow_stage"], "implementation")
        evidence = self.events(lot)[-1]["evidence"]
        self.assertTrue(evidence["verification"]["required"])
        self.assertEqual(evidence["verification"]["verifier"], pr.HOLD_VERIFIER)
        with self.refusal("hold-redundant"):
            self.apply(self.command(lot, action="hold", hold="blocked",
                                    frm="", to="", guard="", version=held.version))
        released = self.apply(self.command(lot, action="release", frm="", to="",
                                           guard="", version=held.version))
        self.assertEqual(released.state, "build")
        self.assertEqual(released.workflow_stage, "implementation")
        resumed = self.events(lot)[-1]["evidence"]
        self.assertTrue(resumed["verification"]["required"])
        self.assertEqual(resumed["verification"]["verifier"], pr.HOLD_VERIFIER)

    def test_release_without_hold_refused(self):
        lot = self.pinned()
        with self.refusal("not-held"):
            self.apply(self.command(lot, action="release", frm="", to="", guard=""))

    def test_waiting_human_requires_an_open_decision(self):
        lot = self.pinned(stage="implementation", state="build")
        cmd = self.command(lot, action="hold", hold="waiting_human",
                           frm="", to="", guard="")
        with self.refusal("hold-no-decision"):
            self.apply(cmd)
        self.request_open_for(lot)
        held = self.apply(cmd)
        self.assertEqual(held.state, "waiting_human")
        self.assertEqual(self.row(lot)["workflow_stage"], "implementation")

    def test_waiting_human_after_closed_answer_refused(self):
        lot = self.pinned(stage="implementation", state="build")
        request = self.request_open_for(lot)
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now(), status = 'answered'"
                        " WHERE id = %s", (request,))
        with self.refusal("hold-no-decision"):
            self.apply(self.command(lot, action="hold", hold="waiting_human",
                                    frm="", to="", guard=""))

    def test_hold_requires_the_installed_verifier(self):
        lot = self.pinned(stage="implementation", state="build")
        with self.refusal("verifier-uninstalled"):
            self.apply(self.command(lot, action="hold", hold="blocked",
                                    frm="", to="", guard=""), verifiers={})
        self.assertEqual(self.events(lot), [])
        self.assertEqual(self.row(lot)["state"], "build")

    def test_release_requires_the_installed_verifier(self):
        lot = self.pinned(stage="implementation", state="build")
        held = self.apply(self.command(lot, action="hold", hold="blocked",
                                       frm="", to="", guard=""))
        with self.refusal("verifier-uninstalled"):
            self.apply(self.command(lot, action="release", frm="", to="", guard="",
                                    version=held.version), verifiers={})
        self.assertEqual(self.row(lot)["state"], "blocked")
        self.assertEqual(len(self.events(lot)), 1)

    def test_waiting_human_release_waits_for_answer_and_verifier(self):
        lot = self.pinned(stage="implementation", state="build")
        request = self.request_open_for(lot)
        held = self.apply(self.command(lot, action="hold", hold="waiting_human",
                                       frm="", to="", guard=""))
        with self.refusal("decision-pending"):
            self.apply(self.command(lot, action="release", frm="", to="", guard="",
                                    version=held.version))
        self.db.execute(
            "UPDATE agent_mailbox SET delivered_at = now(), status = 'answered'"
            " WHERE id = %s", (request,))
        with self.refusal("verifier-uninstalled"):
            self.apply(self.command(lot, action="release", frm="", to="", guard="",
                                    version=held.version), verifiers={})
        self.assertEqual(self.row(lot)["state"], "waiting_human")
        released = self.apply(self.command(lot, action="release", frm="", to="",
                                           guard="", version=held.version))
        self.assertEqual(released.state, "build")

    def test_advance_from_a_hold_refused(self):
        for hold in pr.HOLDS:
            with self.subTest(hold=hold):
                lot = self.pinned(stage="implementation", state="build")
                if hold == "waiting_human":
                    self.request_open_for(lot)
                held = self.apply(self.command(lot, action="hold", hold=hold,
                                               frm="", to="", guard=""))
                with self.refusal("held"):
                    self.apply(self.command(lot, frm="implementation", to="review",
                                            guard="review_requested",
                                            version=held.version))
                self.assertEqual(self.row(lot)["state"], hold)
                self.assertEqual(len(self.events(lot)), 1)

    def test_hold_verifier_that_refuses(self):
        lot = self.pinned(stage="implementation", state="build")

        def refuses(context):
            return pr.Verification(ok=False, reason="hold not proven")

        verifiers = dict(self.verifiers)
        verifiers[pr.HOLD_VERIFIER] = refuses
        with self.refusal("verifier-refused"):
            self.apply(self.command(lot, action="hold", hold="blocked",
                                    frm="", to="", guard=""), verifiers=verifiers)
        self.assertEqual(self.events(lot), [])

    def test_hold_boolean_refused(self):
        lot = self.pinned(stage="implementation", state="build")
        verifiers = dict(self.verifiers)
        verifiers[pr.HOLD_VERIFIER] = lambda context: True
        with self.refusal("verifier-invalid"):
            self.apply(self.command(lot, action="hold", hold="blocked",
                                    frm="", to="", guard=""), verifiers=verifiers)
        self.assertEqual(self.events(lot), [])

    def test_hold_verifier_context(self):
        lot = self.pinned(stage="implementation", state="build")
        seen = []

        def capture(context):
            seen.append((context.command.action, context.command.hold,
                         str(context.work_item["state"]), context.from_stage,
                         context.to_stage))
            return pr.Verification(ok=True, reason="seen")

        verifiers = dict(self.verifiers)
        verifiers[pr.HOLD_VERIFIER] = capture
        held = self.apply(self.command(lot, action="hold", hold="blocked",
                                       frm="", to="", guard=""), verifiers=verifiers)
        self.apply(self.command(lot, action="release", frm="", to="", guard="",
                                version=held.version), verifiers=verifiers)
        self.assertEqual(
            seen,
            [("hold", "blocked", "build", "implementation", "implementation"),
             ("release", "", "blocked", "implementation", "implementation")])

    def test_answer_and_hold_do_not_deadlock(self):
        """N6/N12: the hold and decision closure serialize on the request.

        The hold locks the lot, then the open request. Closing the request
        (`decisions.close`) commits before `decisions.answer` moves the lot:
        the two serialize, and the outcome stays consistent (hold accepted
        if it saw the request, otherwise `hold-no-decision`)."""
        lot = self.pinned(stage="implementation", state="build")
        request = self.request_open_for(lot)

        def hold(db):
            return pr._apply(db, self.command(lot, action="hold",
                                              hold="waiting_human",
                                              frm="", to="", guard=""),
                             spec=self.spec, verifiers=self.verifiers)

        def answer(db):
            return storage.of(db).decisions.close(
                request, status="answered",
                record={"state": "answered",
                        "answer": {"by": "human:acme", "channel": "terminal",
                                   "text": "ok", "option": None}})

        results, errors = self._concurrent([hold, answer])
        self.assertEqual(len(results) + len(errors), 2)
        self.assertEqual([e for e in errors
                          if not isinstance(e, pr.RegistryRefusal)], [])
        for exc in errors:
            self.assertEqual(exc.code, "hold-no-decision")
        closed = self.db.query(
            "SELECT delivered_at FROM agent_mailbox WHERE id = %s",
            (request,))[0]["delivered_at"]
        self.assertIsNotNone(closed)
        state = self.row(lot)["state"]
        if errors:                       # the answer went first
            self.assertEqual(state, "build")
            self.assertEqual(self.events(lot), [])
        else:                            # the hold saw the open request
            self.assertEqual(state, "waiting_human")

    # -- side effects of a released core (N9) ------------------------------

    def test_advance_to_closed_carries_the_closure(self):
        lot = self.pinned()
        receipt = self.apply(self.command(lot, to="stopped", guard="stop_work",
                                          facts={"close_reason": "abandoned"}))
        self.assertEqual(receipt.state, "closed")
        row = self.row(lot)
        self.assertEqual(row["close_reason"], "abandoned")
        self.assertIsNone(row["superseded_by"])
        self.assertIsNotNone(row["closed_at"])
        closure = self.closure_milestones(lot)
        self.assertEqual([m["kind"] for m in closure], ["closed"])
        self.assertEqual(closure[0]["actor"], ACME_ACTOR)
        self.assertIn("abandoned", closure[0]["note"])
        # the receipt remains the last line of the lot's journal
        self.assertEqual(self.journal_version(lot), receipt.event_id)

    def test_advance_to_closed_refuses_an_unproven_closure(self):
        lot = self.pinned()
        with self.refusal("close-reason"):
            self.apply(self.command(lot, to="stopped", guard="stop_work"))
        with self.refusal("close-reason"):
            self.apply(self.command(lot, to="stopped", guard="stop_work",
                                    facts={"close_reason": "finished"}))
        with self.refusal("superseded-unknown"):
            self.apply(self.command(lot, to="stopped", guard="stop_work",
                                    facts={"close_reason": "superseded",
                                           "superseded_by": 0}))
        with self.refusal("superseded-unknown"):
            self.apply(self.command(lot, to="stopped", guard="stop_work",
                                    facts={"close_reason": "superseded",
                                           "superseded_by": lot}))
        with self.refusal("superseded-unknown"):
            self.apply(self.command(lot, to="stopped", guard="stop_work",
                                    facts={"close_reason": "superseded",
                                           "superseded_by": 999999}))
        with self.refusal("delivered-sha"):
            self.apply(self.command(lot, to="stopped", guard="stop_work",
                                    facts={"close_reason": "delivered"}))
        with self.refusal("delivered-sha"):
            self.apply(self.command(lot, to="stopped", guard="stop_work",
                                    facts={"close_reason": "delivered", "sha": "zz"}))
        self.assertEqual(self.events(lot), [])
        self.assertEqual(self.row(lot)["state"], "intake")
        self.assertEqual(self.closure_milestones(lot), [])

    def test_advance_to_closed_bounds_and_normalizes_superseded_by(self):
        """#5027: a huge or padded superseded_by is refused or normalized.

        A reference beyond the CPython int/str limit (or the bigint range) is
        the named `superseded-unknown` refusal, never an uncaught ValueError
        or an out-of-range SQL parameter; the surrounding spaces accepted by
        the verifier are normalized to the lot id."""
        superseding = self.pinned()
        lot = self.pinned()
        with self.refusal("superseded-unknown"):
            self.apply(self.command(lot, to="stopped", guard="stop_work",
                                    facts={"close_reason": "superseded",
                                           "superseded_by": "9" * 4301}))
        with self.refusal("superseded-unknown"):
            self.apply(self.command(lot, to="stopped", guard="stop_work",
                                    facts={"close_reason": "superseded",
                                           "superseded_by": str(2 ** 63)}))
        self.assertEqual(self.events(lot), [])
        self.assertEqual(self.row(lot)["state"], "intake")
        self.apply(self.command(lot, to="stopped", guard="stop_work",
                                facts={"close_reason": "superseded",
                                       "superseded_by": " %d " % superseding}))
        row = self.row(lot)
        self.assertEqual(row["superseded_by"], superseding)
        self.assertEqual(row["state"], "closed")

    def test_advance_to_closed_records_superseded_by_and_delivered(self):
        superseding = self.pinned()
        lot = self.pinned()
        self.apply(self.command(lot, to="stopped", guard="stop_work",
                                facts={"close_reason": "superseded",
                                       "superseded_by": superseding}))
        row = self.row(lot)
        self.assertEqual(row["close_reason"], "superseded")
        self.assertEqual(row["superseded_by"], superseding)
        self.assertEqual([m["kind"] for m in self.closure_milestones(lot)], ["closed"])

        delivered = self.pinned()
        head = "a1b2c3d4" * 5
        self.apply(self.command(delivered, to="stopped", guard="stop_work",
                                facts={"close_reason": "delivered",
                                       "sha": head.upper()}))
        kinds = {m["kind"]: m for m in self.closure_milestones(delivered)}
        self.assertEqual(set(kinds), {"closed", "delivered"})
        self.assertEqual(kinds["delivered"]["sha"], head)

    def test_advance_to_a_released_core_gives_the_migration_back(self):
        lot = self.pinned(stage="review", state="qa")
        self.db.execute("UPDATE work_items SET migration = 452,"
                        " migration_repo = 'manaty/ameesh' WHERE id = %s", (lot,))
        receipt = self.apply(self.command(lot, frm="review", to="staging",
                                          guard="qa_started", version=0))
        self.assertEqual(receipt.state, "merged")
        row = self.row(lot)
        self.assertIsNone(row["migration"])
        self.assertIsNone(row["migration_repo"])
        released = [e for e in self.events(lot) if e["command_id"] is None]
        self.assertEqual(len(released), 1)
        self.assertIn("migration 452 released", released[0]["note"])
        self.assertEqual(self.journal_version(lot), receipt.event_id)

    def test_first_release_wakes_the_dependants(self):
        blocker = self.pinned(stage="review", state="qa")
        self.agent("acme-dev2")
        dependent = self.dependent(blocker)
        self.apply(self.command(blocker, frm="review", to="staging",
                                guard="qa_started", version=0))
        mails = self.mail_to("acme-dev2")
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails[0]["payload"]["dependency"],
                         {"dependent": dependent, "blocker": blocker,
                          "blocker_state": "merged"})
        self.assertIn("is merged", mails[0]["body"])
        self.assertEqual(self.row(dependent)["state"], "blocked")

    def test_advance_to_promoted_wakes_with_its_own_state(self):
        # A path that goes straight to the `promoted` core (without a `merged`
        # stage): the wake-up cannot say "merged", it names the state reached.
        spec = _Spec(transitions=ACME_TRANSITIONS
                     + (_Transition("review", "done", "work_completed"),))
        blocker = self.pinned(stage="review", state="qa")
        self.agent("acme-dev2")
        dependent = self.dependent(blocker)
        self.apply(self.command(blocker, frm="review", to="done",
                                guard="work_completed", version=0), spec=spec)
        mails = self.mail_to("acme-dev2")
        self.assertEqual(len(mails), 1)
        self.assertEqual(mails[0]["payload"]["dependency"]["blocker_state"], "promoted")
        self.assertIn("is promoted", mails[0]["body"])
        self.assertEqual(self.row(blocker)["state"], "delivered")
        self.assertEqual(self.row(blocker)["legacy_state"], "promoted")
        self.assertEqual(self.row(dependent)["state"], "blocked")

    def test_a_lot_already_released_does_not_wake_twice(self):
        blocker = self.pinned(stage="review", state="qa")
        self.agent("acme-dev2")
        self.dependent(blocker)
        merged = self.apply(self.command(blocker, frm="review", to="staging",
                                         guard="qa_started", version=0))
        promoted = self.apply(self.command(blocker, frm="staging", to="done",
                                           guard="work_completed",
                                           version=merged.version))
        self.assertEqual(promoted.state, "promoted")
        self.assertEqual(len(self.mail_to("acme-dev2")), 1)

    def test_unassigned_dependent_is_reopened_in_the_same_transaction(self):
        blocker = self.pinned(stage="review", state="qa")
        dependent = self.dependent(blocker, assignee=None)
        self.apply(self.command(blocker, frm="review", to="staging",
                                guard="qa_started", version=0))
        self.assertEqual(self.row(dependent)["state"], "intake")
        self.assertEqual(self.events(dependent)[-1]["state"], "intake")
        self.assertEqual(self.mail_to("acme-dev2"), [])

    def test_side_effects_roll_back_together(self):
        lot = self.pinned()
        self.db.execute("UPDATE work_items SET migration = 452,"
                        " migration_repo = 'acme/repo' WHERE id = %s", (lot,))
        self.db.script(
            "CREATE FUNCTION t_1926_boom() RETURNS trigger LANGUAGE plpgsql AS $$"
            " BEGIN RAISE EXCEPTION 'boom 1926'; END $$;"
            " CREATE TRIGGER t_1926_boom BEFORE INSERT ON work_item_milestones"
            "   FOR EACH ROW EXECUTE FUNCTION t_1926_boom();")
        try:
            with self.assertRaises(DbError):
                self.apply(self.command(lot, to="stopped", guard="stop_work",
                                        facts={"close_reason": "abandoned"}))
        finally:
            self.db.script("DROP TRIGGER t_1926_boom ON work_item_milestones;"
                           " DROP FUNCTION t_1926_boom();")
        row = self.row(lot)
        self.assertEqual(row["state"], "intake")
        self.assertEqual(row["migration"], 452)
        self.assertIsNone(row["close_reason"])
        self.assertEqual(self.closure_milestones(lot), [])
        self.assertEqual(self.events(lot), [])

    def test_failed_wake_rolls_back_the_closure(self):
        blocker = self.pinned(stage="review", state="qa")
        self.agent("acme-dev2")
        self.dependent(blocker)
        self.db.execute("UPDATE work_items SET migration = 452,"
                        " migration_repo = 'acme/repo' WHERE id = %s", (blocker,))
        self.db.script(
            "CREATE FUNCTION t_1926_nomail() RETURNS trigger LANGUAGE plpgsql AS $$"
            " BEGIN RAISE EXCEPTION 'no mail 1926'; END $$;"
            " CREATE TRIGGER t_1926_nomail BEFORE INSERT ON agent_mailbox"
            "   FOR EACH ROW EXECUTE FUNCTION t_1926_nomail();")
        try:
            with self.assertRaises(DbError):
                self.apply(self.command(blocker, frm="review", to="staging",
                                        guard="qa_started", version=0))
        finally:
            self.db.script("DROP TRIGGER t_1926_nomail ON agent_mailbox;"
                           " DROP FUNCTION t_1926_nomail();")
        row = self.row(blocker)
        self.assertEqual(row["state"], "qa")
        self.assertEqual(row["migration"], 452)
        self.assertEqual(self.closure_milestones(blocker), [])
        self.assertEqual(self.events(blocker), [])
        self.assertEqual(self.mail_to("acme-dev2"), [])

    # -- revision transfer ------------------------------------------------

    def transfer(self, lot_id: int, *, ref: str | None = None, version: int = ACME_VERSION,
                 spec=None, expected: int | None = None, command_id: str | None = None,
                 verifiers=None):
        return pr._transfer(
            self.db, work_item_id=lot_id, command_id=command_id or str(uuid.uuid4()),
            expected_version=self.journal_version(lot_id) if expected is None else expected,
            workflow_ref=ref or self.spec.workflow_ref, workflow_version=version,
            authority=self.authority(), spec=spec or self.spec,
            evidence={"review": "relu"},
            verifiers=self.verifiers if verifiers is None else verifiers)

    def test_explicit_adoption_of_a_workflow(self):
        lot = self.lot()                       # no workflow
        receipt = self.transfer(lot)
        self.assertEqual(receipt.action, "transfer")
        row = self.row(lot)
        self.assertEqual(row["workflow_ref"], self.spec.workflow_ref)
        self.assertEqual(row["workflow_version"], ACME_VERSION)
        self.assertEqual(row["workflow_stage"], "draft")
        self.assertEqual(row["state"], "intake")
        self.assertIn(pr.TRANSFER_VERIFIER, self.calls)
        event = self.events(lot)[-1]
        self.assertTrue(event["evidence"]["verification"]["required"])
        self.assertEqual(event["evidence"]["verification"]["verifier"],
                         pr.TRANSFER_VERIFIER)
        self.assertEqual(event["evidence"]["command"]["previous_ref"], None)

    def test_adoption_refuses_a_terminal_lot(self):
        for state in sorted(pr.TERMINAL_CORES):
            with self.subTest(state=state):
                lot = self.lot()                   # no workflow
                self.db.execute(
                    "UPDATE work_items SET state = %s, close_reason = %s WHERE id = %s",
                    (state, "abandoned" if state == "closed" else None, lot))
                with self.refusal("terminal-reopened"):
                    self.transfer(lot)
                self.assertEqual(self.events(lot), [])
                row = self.row(lot)
                self.assertEqual(row["state"], lifecycle.canonical(state))
                self.assertIsNone(row["workflow_ref"])
                self.assertEqual(row["workflow_version"], 0)

    def test_adoption_refuses_a_different_core(self):
        # Adoption does not move the native state back: the initial stage must
        # project the current core (a hold, however, is preserved).
        for state in ("build", "qa", "merged"):
            with self.subTest(state=state):
                lot = self.lot(state=state)        # no workflow
                with self.refusal("stage-mismatch"):
                    self.transfer(lot)
                self.assertEqual(self.events(lot), [])
                self.assertEqual(self.row(lot)["state"], lifecycle.canonical(state))
        held = self.lot(state="blocked")           # hold: core to be proven
        with self.refusal("hold-core-unproven"):   # N10: silent journal
            self.transfer(held)
        self.assertEqual(self.events(held), [])
        proven = self.lot(state="blocked")
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'intake', 'acme avant la retenue', %s)", (proven, ACME_ACTOR))
        receipt = self.transfer(proven)            # hold preserved (N10)
        self.assertEqual(receipt.state, "blocked")
        self.assertEqual(receipt.workflow_stage, "draft")

    def test_transfer_without_verifier_refused(self):
        lot = self.lot()
        with self.refusal("verifier-uninstalled"):
            self.transfer(lot, verifiers={})
        self.assertIsNone(self.row(lot)["workflow_ref"])

    def test_transfer_keeps_the_stage_and_does_not_regress(self):
        lot = self.pinned(stage="review", state="qa")
        newer = _Spec(version=ACME_VERSION + 1)
        receipt = self.transfer(lot, version=ACME_VERSION + 1, spec=newer)
        row = self.row(lot)
        self.assertEqual(row["workflow_version"], ACME_VERSION + 1)
        self.assertEqual(row["workflow_stage"], "review")
        self.assertEqual(row["state"], "qa")
        self.assertEqual(receipt.version, self.journal_version(lot))
        with self.refusal("revision-regression"):
            self.transfer(lot, version=ACME_VERSION, expected=self.journal_version(lot))
        self.assertEqual(self.row(lot)["workflow_version"], ACME_VERSION + 1)

    def test_transfer_refuses_an_inconsistent_core(self):
        lot = self.pinned(stage="review", state="qa")
        shifted = _Spec(stages=(
            _Stage("draft", "intake"),
            _Stage("implementation", "build"),
            _Stage("review", "build"),          # same status, different core
            _Stage("staging", "merged"),
            _Stage("done", "promoted"),
            _Stage("stopped", "closed"),
        ))
        with self.refusal("stage-mismatch"):
            self.transfer(lot, spec=shifted)
        self.assertEqual(self.row(lot)["workflow_ref"], self.spec.workflow_ref)
        self.assertEqual(self.row(lot)["workflow_version"], ACME_VERSION)

    def test_after_transfer_the_old_revision_is_refused(self):
        lot = self.pinned()
        first = self.apply(self.command(lot))
        self.transfer(lot, version=ACME_VERSION + 1, spec=_Spec(version=ACME_VERSION + 1))
        with self.refusal("revision-mismatch"):
            self.apply(self.command(lot, frm="implementation", to="review",
                                    guard="review_requested", version=first.version))
        # variant: a different reference (the lot stays on the old one)
        with self.refusal("ref-mismatch"):
            self.apply(self.command(lot, ref="acme/process-workflow@other",
                                    frm="implementation", to="review",
                                    guard="review_requested", version=first.version),
                       spec=_Spec(ref="acme/process-workflow@other"))

    def test_transfer_replays(self):
        lot = self.lot()
        cid = str(uuid.uuid4())
        first = self.transfer(lot, command_id=cid, expected=0)
        again = self.transfer(lot, command_id=cid, expected=0)
        self.assertTrue(again.replayed)
        self.assertEqual(again.event_id, first.event_id)
        self.assertEqual(len(self.events(lot)), 1)

    # -- adoption/transfer of a held lot (N10) -----------------------------

    def test_adoption_of_a_held_lot_needs_a_proven_core(self):
        lot = self.lot(state="blocked")            # no journal line
        with self.refusal("hold-core-unproven"):
            self.transfer(lot, spec=_Spec(initial="implementation"))
        self.assertIsNone(self.row(lot)["workflow_ref"])
        self.assertEqual(self.events(lot), [])

    def test_adoption_of_a_held_lot_resumes_into_the_proven_core(self):
        lot = self.lot(state="blocked")
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'build', 'acme avant la retenue', %s)", (lot, ACME_ACTOR))
        receipt = self.transfer(lot, spec=_Spec(initial="implementation"))
        self.assertEqual(receipt.state, "blocked")
        self.assertEqual(receipt.workflow_stage, "implementation")
        released = self.apply(self.command(lot, action="release", frm="", to="", guard="",
                                           version=receipt.version))
        self.assertEqual(released.state, "build")
        self.assertEqual(released.workflow_stage, "implementation")

    def test_adoption_of_a_held_lot_refuses_a_divergent_core(self):
        lot = self.lot(state="blocked")
        self.db.execute(
            "INSERT INTO work_item_events (work_item_id, state, note, actor)"
            " VALUES (%s, 'build', 'acme avant la retenue', %s)", (lot, ACME_ACTOR))
        with self.refusal("stage-mismatch"):
            self.transfer(lot, spec=_Spec(initial="draft"))       # intake core
        self.assertIsNone(self.row(lot)["workflow_ref"])

    def test_transfer_of_a_held_lot_proves_the_kept_core(self):
        lot = self.lot(state="build")
        first = self.transfer(lot, spec=_Spec(initial="implementation"))
        self.apply(self.command(lot, action="hold", hold="blocked", frm="", to="",
                                guard="", version=first.version))
        same = _Spec(version=ACME_VERSION + 1)
        accepted = pr._transfer(
            self.db, work_item_id=lot, command_id=str(uuid.uuid4()),
            expected_version=self.journal_version(lot),
            workflow_ref=same.workflow_ref, workflow_version=same.workflow_version,
            authority=self.authority(), spec=same, verifiers=self.verifiers)
        self.assertEqual(accepted.state, "blocked")
        self.assertEqual(accepted.workflow_version, same.workflow_version)
        moved = _Spec(version=ACME_VERSION + 2, stages=tuple(
            _Stage(stage.status, "qa" if stage.status == "implementation" else stage.core)
            for stage in ACME_STAGES))
        with self.refusal("stage-mismatch"):
            pr._transfer(
                self.db, work_item_id=lot, command_id=str(uuid.uuid4()),
                expected_version=self.journal_version(lot),
                workflow_ref=moved.workflow_ref, workflow_version=moved.workflow_version,
                authority=self.authority(), spec=moved, verifiers=self.verifiers)

    # -- lots without workflow, history, rollback --------------------------

    def test_lot_without_workflow_unchanged(self):
        lot = self.lot(state="build")
        before = self.events(lot)
        with self.refusal("workflow-absent"):
            self.apply(self.command(lot))
        work.move(self.db, lot, "qa", actor=ACME_ACTOR)
        row = self.row(lot)
        self.assertEqual(row["state"], "qa")
        self.assertIsNone(row["workflow_ref"])
        self.assertIsNone(row["workflow_stage"])
        self.assertEqual(row["workflow_version"], 0)
        after = self.events(lot)
        self.assertEqual([e["id"] for e in before], [e["id"] for e in after][:len(before)])
        self.assertTrue(all(e["command_id"] is None for e in after))

    def test_existing_history_intact(self):
        lot = self.lot(state="build")
        work.move(self.db, lot, "qa", actor=ACME_ACTOR, note="revue demandée")
        before = self.events(lot)
        pinned = self.pinned()
        self.apply(self.command(pinned))
        after = self.events(lot)
        self.assertEqual(before, after)
        self.assertIsNone(after[0]["command_id"])
        self.assertIsNone(after[0]["evidence"])

    def test_rollback_without_half_event(self):
        lot = self.pinned()
        self.db.script(
            "CREATE FUNCTION t_1922_boom() RETURNS trigger LANGUAGE plpgsql AS $$"
            " BEGIN RAISE EXCEPTION 'boom 1922'; END $$;"
            " CREATE TRIGGER t_1922_boom BEFORE UPDATE ON work_items"
            "   FOR EACH ROW EXECUTE FUNCTION t_1922_boom();")
        try:
            with self.assertRaises(DbError):
                self.apply(self.command(lot))
        finally:
            self.db.script("DROP TRIGGER t_1922_boom ON work_items;"
                           " DROP FUNCTION t_1922_boom();")
        row = self.row(lot)
        self.assertEqual(row["state"], "intake")
        self.assertEqual(row["workflow_stage"], "draft")
        self.assertEqual(self.events(lot), [])

    def test_verifier_failure_rolls_back_everything(self):
        lot = self.pinned()

        def explodes(context):
            raise RuntimeError("installed component failure")

        with self.refusal("verifier-error"):
            self.apply(self.command(lot), verifiers={"planning": explodes})
        self.assertEqual(self.events(lot), [])
        self.assertEqual(self.row(lot)["state"], "intake")

    # -- concurrency -------------------------------------------------------

    def _concurrent(self, functions):
        results, errors = [], []
        barrier = threading.Barrier(len(functions), timeout=15)

        def wrap(function):
            def run():
                db = self.connect()
                try:
                    barrier.wait()
                    results.append(function(db))
                except Exception as exc:
                    errors.append(exc)
                finally:
                    db.close()
            return run

        threads = [threading.Thread(target=wrap(f)) for f in functions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertFalse(any(t.is_alive() for t in threads), "thread blocked")
        return results, errors

    def test_concurrent_same_key_single_receipt(self):
        lot = self.pinned()
        cmd = self.command(lot)

        def run(db):
            return pr._apply(db, cmd, spec=self.spec, verifiers=self.verifiers)

        results, errors = self._concurrent([run, run])
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.accepted for r in results))
        self.assertEqual(sum(1 for r in results if r.replayed), 1)
        self.assertEqual(len(self.events(lot)), 1)
        self.assertEqual(self.calls, ["planning"])

    def test_two_commands_same_version_only_one_wins(self):
        lot = self.pinned()
        first = self.command(lot)
        second = self.command(lot)

        def run_a(db):
            return pr._apply(db, first, spec=self.spec, verifiers=self.verifiers)

        def run_b(db):
            return pr._apply(db, second, spec=self.spec, verifiers=self.verifiers)

        results, errors = self._concurrent([run_a, run_b])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], pr.RegistryRefusal)
        self.assertEqual(errors[0].code, "version-conflict")
        self.assertEqual(len(self.events(lot)), 1)

    def test_lot_lock_before_decision_requests(self):
        """N12: the lot first, then the decision requests — L233's order.

        A concurrent `ask` holds the lot and inserts its request in the SAME
        transaction; the release command waits for the lot, then locks the
        request and reads it back: it refuses (`decision-pending`), without
        deadlocking. The probe checks the request while waiting: under the
        reverse order (request then lot), the command would hold the request
        while waiting for the lot, the `ask` would request it in turn and both
        would block — the probe would see it and the test would fail.
        """
        lot = self.pinned(stage="implementation", state="waiting_human")
        request = self.request_open_for(lot)
        b_ready = threading.Event()
        a_started = threading.Event()
        b_committed = threading.Event()
        outcome: dict = {}

        def ask():
            db, probe = self.connect(), self.connect()
            try:
                with db.transaction() as tx:
                    tx.execute("UPDATE work_items SET updated_at = now() WHERE id = %s",
                               (lot,))
                    tx.execute(
                        "INSERT INTO agent_mailbox (sender, recipient, body, kind,"
                        " payload, work_item_id) VALUES ('acme-orch', 'human:*', 'ask',"
                        " 'request', '{\"decision\": {\"state\": \"pending\"}}'::jsonb, %s)",
                        (str(lot),))
                    b_ready.set()
                    self.assertTrue(a_started.wait(15), "the command did not start")
                    deadline = time.time() + 3
                    held = False
                    while time.time() < deadline:
                        try:
                            probe.query(
                                "SELECT id FROM agent_mailbox WHERE id = %s"
                                " FOR UPDATE NOWAIT", (request,))
                        except DbError as exc:
                            if "could not obtain lock" not in str(exc):
                                raise
                            held = True
                            break
                    outcome["held"] = held
                    if held:
                        # the command holds the request: we request it too
                        tx.query("SELECT id FROM agent_mailbox WHERE id = %s"
                                 " FOR UPDATE", (request,))
            finally:
                probe.close()
                db.close()
                b_committed.set()

        errors: list = []
        results: list = []

        def run_ask():
            try:
                ask()
            except Exception as exc:            # noqa: BLE001 - propagated to the test
                errors.append(exc)

        thread = threading.Thread(target=run_ask)
        thread.start()
        self.assertTrue(b_ready.wait(15), "ask did not lock the lot")
        a_started.set()
        try:
            results.append(pr._apply(
                self.db, self.command(lot, action="release", frm="", to="", guard=""),
                spec=self.spec, verifiers=self.verifiers))
        except Exception as exc:                # noqa: BLE001 - propagated to the test
            errors.append(exc)
        thread.join(30)
        self.assertFalse(thread.is_alive(), "ask blocked")
        self.assertFalse(outcome.get("held"),
                         "the command held the request before the lot: reverse order")
        self.assertEqual([e for e in errors
                          if not isinstance(e, pr.RegistryRefusal)], [])
        self.assertEqual([e.code for e in errors
                          if isinstance(e, pr.RegistryRefusal)], ["decision-pending"])
        self.assertEqual(results, [])
        self.assertTrue(b_committed.is_set())
        self.assertEqual(self.row(lot)["state"], "waiting_human")

    # -- reading and migration constraints ---------------------------------

    def test_registry_state_and_receipt(self):
        lot = self.pinned()
        cmd = self.command(lot)
        state = pr.registry_state(self.db, lot)
        self.assertEqual((state.workflow_ref, state.workflow_stage,
                          state.workflow_version, state.version, state.receipts),
                         (ACME_REF, "draft", ACME_VERSION, 0, 0))
        receipt = self.apply(cmd)
        state = pr.registry_state(self.db, lot)
        self.assertEqual(state.version, receipt.event_id)
        self.assertEqual(state.receipts, 1)
        stored = pr.receipt_of(self.db, lot, cmd.command_id)
        self.assertEqual(stored["workflow_stage"], "implementation")
        self.assertEqual(stored["evidence"]["intent"]["sha256"],
                         pr.intent_sha256(cmd))
        self.assertIsNone(pr.receipt_of(self.db, lot, str(uuid.uuid4())))

    def test_migration_receipt_uniqueness_and_completeness(self):
        lot = self.pinned()
        cmd = self.command(lot)
        receipt = self.apply(cmd)
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_item_events (work_item_id, state, command_id)"
                " VALUES (%s, 'build', %s::uuid)",
                (lot, str(uuid.uuid4())))
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_item_events (work_item_id, state, command_id,"
                "  workflow_stage, workflow_version, evidence)"
                " VALUES (%s, 'build', %s::uuid, 'implementation', %s, %s::jsonb)",
                (lot, cmd.command_id, ACME_VERSION, json.dumps(
                    {"schema": pr.RECEIPT_SCHEMA, "intent": {"sha256": "0" * 64},
                     "command": {"action": "advance"},
                     "authority": {}, "verification": {}})))
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_item_events (work_item_id, state, command_id,"
                "  workflow_stage, workflow_version, evidence)"
                " VALUES (%s, 'build', %s::uuid, 'implementation', %s, %s::jsonb)",
                (lot, str(uuid.uuid4()), ACME_VERSION, json.dumps(
                    {"schema": pr.RECEIPT_SCHEMA, "intent": {"sha256": "0" * 64},
                     "command": {"action": "teleport"},
                     "authority": {}, "verification": {}})))
        self.assertEqual(len(self.events(lot)), 1)
        self.assertEqual(pr.receipt_of(self.db, lot, cmd.command_id)["id"],
                         receipt.event_id)

    def test_migration_complete_receipt_requires_every_field(self):
        """`IS TRUE`: any omission — missing key or nested `null` — is refused."""
        lot = self.pinned()
        full = {"schema": pr.RECEIPT_SCHEMA, "intent": {"sha256": "0" * 64},
                "command": {"action": "advance"},
                "authority": {}, "verification": {}}

        def insert(evidence):
            self.db.execute(
                "INSERT INTO work_item_events (work_item_id, state, command_id,"
                "  workflow_stage, workflow_version, evidence)"
                " VALUES (%s, 'build', %s::uuid, 'implementation', %s, %s::jsonb)",
                (lot, str(uuid.uuid4()), ACME_VERSION, json.dumps(evidence)))

        insert(full)                      # positive control: the full shape passes
        self.assertEqual(len(self.events(lot)), 1)
        for field in ("schema", "intent", "command", "authority", "verification"):
            with self.subTest(omission=field):
                with self.assertRaises(DbError):
                    insert({k: v for k, v in full.items() if k != field})
        refusals = (
            {},                                      # empty receipt, all fields NULL
            {"schema": None, "intent": full["intent"], "command": full["command"],
             "authority": {}, "verification": {}},
            dict(full, intent=None),
            dict(full, intent={}),                   # sha256 missing (nested)
            dict(full, intent={"sha256": None}),
            dict(full, command={}),                  # action missing (nested)
            dict(full, command={"action": None}),
            dict(full, authority=None),
            dict(full, verification=None),
        )
        for evidence in refusals:
            with self.subTest(evidence=json.dumps(evidence, sort_keys=True)):
                with self.assertRaises(DbError):
                    insert(evidence)
        self.assertEqual(len(self.events(lot)), 1)

    def test_migration_reapplied_reinstalls_the_hardened_constraint(self):
        """N11 (Claude review): a bench database that had applied 0452
        before the hardening kept the old lax CHECK; reapplying the file
        starts again from the current definition
        (`drop constraint` + `add`)."""
        lot = self.pinned()
        self.db.execute("ALTER TABLE work_item_events"
                        " DROP CONSTRAINT work_item_events_receipt_complete")
        self.db.execute(
            "ALTER TABLE work_item_events ADD CONSTRAINT"
            " work_item_events_receipt_complete CHECK (command_id is null"
            "  or evidence->>'schema' = 'process-receipt/1')")
        [path] = glob.glob(os.path.join(REPO, "src", "ameesh", "migrations",
                                        "0452_*.sql"))
        with open(path, encoding="utf-8") as fh:
            self.db.script(fh.read())
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_item_events (work_item_id, state, command_id,"
                "  workflow_stage, workflow_version, evidence)"
                " VALUES (%s, 'build', %s::uuid, 'implementation', %s, '{}'::jsonb)",
                (lot, str(uuid.uuid4()), ACME_VERSION))
        self.assertEqual(self.events(lot), [])

    def test_migration_stage_without_reference_refused(self):
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_items (title, state, workflow_stage)"
                " VALUES ('acme : étape orpheline', 'intake', 'draft')")

    def test_migration_negative_version_refused(self):
        with self.assertRaises(DbError):
            self.db.execute(
                "INSERT INTO work_items (title, state, workflow_ref, workflow_version)"
                " VALUES ('acme : version négative', 'intake', %s, -1)", (ACME_REF,))

    # -- public validation ------------------------------------------------

    def test_malformed_input_refused(self):
        lot = self.pinned()
        with self.refusal("command-invalid"):
            self.apply(self.command(lot, command_id="pas-un-uuid"))
        with self.refusal("command-invalid"):
            self.apply(self.command(lot, action="teleport"))
        with self.refusal("authority-invalid"):
            self.apply(self.command(lot, authority=pr.Authority(actor="")))
        with self.refusal("authority-source"):
            self.apply(self.command(lot, authority=pr.Authority(
                actor=ACME_ACTOR, source="dream")))
        with self.refusal("evidence-invalid"):
            self.apply(self.command(lot, facts={"ratio": 1.5}))
        with self.refusal("hold-invalid"):
            self.apply(self.command(lot, action="hold", hold="paused",
                                    frm="", to="", guard=""))
        with self.refusal("spec-mismatch"):
            self.apply(self.command(lot), spec=_Spec(version=ACME_VERSION + 9))
        with self.refusal("spec-invalid"):
            self.apply(self.command(lot), spec=object())

    def test_install_a_verifier(self):
        lot = self.pinned()
        self.addCleanup(pr.INSTALLED_VERIFIERS.pop, "planning", None)
        with self.refusal("verifier-name"):
            pr.install_verifier("Planning!", _ok)
        pr.install_verifier("planning", _ok)
        self.assertTrue(pr.apply_command(self.db, self.command(lot),
                                         spec=self.spec).accepted)
        with self.refusal("verifier-name"):
            pr.install_verifier("planning", lambda context: _ok())
        pr.install_verifier("planning", _ok)          # idempotent, same object
        self.assertEqual(list(pr.installed_verifiers()), ["planning"])

    def test_public_api_without_verifier_injection(self):
        """A guard's result never comes from the caller (published seam)."""
        lot = self.pinned()
        with self.assertRaises(TypeError):
            pr.apply_command(self.db, self.command(lot), spec=self.spec,
                             verifiers={"planning": _ok})
        with self.assertRaises(TypeError):
            pr.transfer_revision(
                self.db, work_item_id=lot, command_id=str(uuid.uuid4()),
                expected_version=0, workflow_ref=self.spec.workflow_ref,
                workflow_version=ACME_VERSION, authority=self.authority(),
                spec=self.spec, verifiers=self.verifiers)

    def test_move_close_refuse_a_pinned_lot(self):
        lot = self.pinned(stage="implementation", state="build")
        with self.assertRaises(work.WorkError):
            work.move(self.db, lot, "qa")
        with self.assertRaises(work.WorkError):
            work.close(self.db, lot, abandoned=True)
        refused = work.close_merged(self.db, lot)
        self.assertEqual(refused["result"], "refused")
        self.assertIn("process_registry", refused["detail"])
        self.assertEqual(self.row(lot)["state"], "build")
        self.assertEqual(self.events(lot), [])

    def test_legacy_writes_filter_the_pinned_lot_in_sql(self):
        """N8 (Claude review): the application guard reads `workflow_ref`
        outside the lot lock; between that read and the UPDATE, a concurrent
        adoption (which keeps the state) let the legacy move through. The
        filter is in the SQL: the storage layer no longer touches a pinned
        lot, even called with the snapshot from before the adoption."""
        lot = self.pinned(stage="review", state="qa")
        store = storage.of(self.db).work
        self.assertIsNone(store.move(lot, "build", current="qa", loops=1,
                                     note="n", actor="test"))
        self.assertIsNone(store.close(
            lot, current="qa", reason="abandoned", superseded_by=None,
            sha="", actor="test", note="n"))
        self.assertIsNone(store.close_merged(
            lot, current="qa", sha="a" * 40, actor="test", note="n", pr_ref=None))
        self.assertEqual(self.row(lot)["state"], "qa")
        self.assertEqual(self.events(lot), [])

    def test_la_course_avec_une_adoption_nomme_la_retenue(self):
        """N14 (relecture Claude) : entre la lecture applicative (lot non
        pinné) et l'UPDATE, le registre adopte le lot. Le filtre SQL refuse
        (N8) ; le refus legacy doit nommer la retenue, pas un « moved in the
        meantime » générique qui ferait réessayer une écriture réservée au
        registre."""
        lot = self.lot(state="build")
        stale = work.get(self.db, lot)          # instantané non pinné
        self.db.execute(
            "UPDATE work_items SET workflow_ref = %s, workflow_stage = 'implementation',"
            " workflow_version = 1 WHERE id = %s", (self.spec.workflow_ref, lot))
        fresh = work.get(self.db, lot)          # la même lecture, après adoption
        for call in (lambda: work.move(self.db, lot, "qa", actor=ACME_ACTOR),
                     lambda: work.close(self.db, lot, abandoned=True, actor=ACME_ACTOR)):
            with self.subTest(call=call), \
                    mock.patch.object(work, "get", side_effect=[stale, fresh]), \
                    self.assertRaisesRegex(work.WorkError, "pinned to workflow"):
                call()
        self.assertEqual(self.row(lot)["state"], "build")
        self.assertEqual(self.events(lot), [])

    def _restore_workflow_columns(self) -> None:
        """Reapplies the 0452 columns and guards (additive migration)."""
        self.db.execute("ALTER TABLE work_items"
                        " ADD COLUMN IF NOT EXISTS workflow_ref text")
        self.db.execute("ALTER TABLE work_items"
                        " ADD COLUMN IF NOT EXISTS workflow_stage text")
        self.db.execute("ALTER TABLE work_items"
                        " ADD COLUMN IF NOT EXISTS workflow_version bigint"
                        " NOT NULL DEFAULT 0")
        self.db.execute(
            "ALTER TABLE work_items DROP CONSTRAINT IF EXISTS"
            " work_items_workflow_version_nonnegative")
        self.db.execute(
            "ALTER TABLE work_items ADD CONSTRAINT"
            " work_items_workflow_version_nonnegative"
            " CHECK (workflow_version >= 0)")
        self.db.execute(
            "ALTER TABLE work_items DROP CONSTRAINT IF EXISTS"
            " work_items_workflow_stage_needs_ref")
        self.db.execute(
            "ALTER TABLE work_items ADD CONSTRAINT"
            " work_items_workflow_stage_needs_ref"
            " CHECK (workflow_stage is null or workflow_ref is not null"
            " OR (state = 'delivered' AND workflow_stage IN ('merged', 'promoted')))")
        self.db.execute("UPDATE work_items SET legacy_state = state")
        self.db.execute("CREATE TRIGGER aa_work_items_generic_state_sync BEFORE INSERT OR UPDATE"
                        " ON work_items FOR EACH ROW EXECUTE FUNCTION work_items_generic_state_sync()")
        self.db.execute("ALTER TABLE work_items ADD CONSTRAINT work_items_legacy_state_matches"
                        " CHECK (((state = 'delivered' AND legacy_state IN ('merged','promoted'))"
                        " OR (state <> 'delivered' AND legacy_state = state)) IS TRUE)")

    def test_before_0452_historical_commands_do_not_break(self):
        """B5 (package 0650), pre-0452 support: without the registry
        columns, `work.get` returns an empty `workflow_ref` (no lot is
        pinned there) and the historical commands remain those from before
        the registry — the read depends on no column probe."""
        moved = self.lot(state="build")
        to_close = self.lot(state="build")
        to_freeze = self.lot(state="intake")
        self.db.execute("DROP TRIGGER aa_work_items_generic_state_sync ON work_items")
        self.db.execute("ALTER TABLE work_items DROP CONSTRAINT work_items_legacy_state_matches")
        self.db.execute("ALTER TABLE work_items DROP COLUMN workflow_ref CASCADE")
        self.db.execute("ALTER TABLE work_items DROP COLUMN workflow_stage")
        self.db.execute("ALTER TABLE work_items DROP COLUMN workflow_version")
        self.addCleanup(self._restore_workflow_columns)

        self.assertEqual(work.get(self.db, moved)["workflow_ref"], "")
        self.assertEqual(work.get(self.db, to_close)["workflow_ref"], "")
        row = work.move(self.db, moved, "qa", actor=ACME_ACTOR)
        self.assertEqual(row["state"], "qa")
        closed = work.close(self.db, to_close, abandoned=True, actor=ACME_ACTOR)
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(self.row(moved)["state"], "qa")
        self.assertEqual(self.row(to_close)["state"], "closed")
        frozen = storage.of(self.db).work.add_milestone(
            to_freeze, "frozen", sha="a" * 40, actor=ACME_ACTOR,
            verdict=None, note="pre-0452 freeze (#3489)")
        self.assertEqual(frozen["kind"], "frozen")
        self.assertEqual(self.row(to_freeze)["state"], "qa")
        self.assertIn(frozen["id"], [m["id"] for m in
                                   work.milestones(self.db, to_freeze)])

    def test_read_sources_declares_only_the_real(self):
        lot = self.pinned(stage="implementation", state="build")
        receipt = self.apply(self.command(
            lot, frm="implementation", to="review", guard="review_requested",
            facts={"ticket": "ACME-7"}))
        self.request_open_for(lot)
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor, note)"
            " VALUES (%s, 'delivered', %s, %s, 'livré hors cible')",
            (lot, "a" * 40, ACME_ACTOR))
        context = pr.VerificationContext(
            db=self.db, work_item=self.row(lot),
            command=self.command(lot, frm="implementation", to="review",
                                 guard="review_requested", version=receipt.version),
            spec=self.spec, from_stage="implementation", to_stage="review")
        from ameesh import process_approvals as approvals_mod
        from ameesh import process_observations as observations_mod
        with mock.patch.object(
                approvals_mod, "local_projection",
                side_effect=approvals_mod.ProjectionError(
                    "projection-missing", "test bench")), \
                mock.patch.object(
                    observations_mod, "local_projection",
                    side_effect=observations_mod.ProjectionError(
                        "projection-missing", "test bench")):
            sources = pr.read_sources(context)
        self.assertEqual(set(sources),
                         set(pr.SOURCE_KEYS) - {"approvals", "observations"})
        self.assertNotIn("observations", sources)      # no projection here
        self.assertNotIn("approvals", sources)         # no projection here
        got = sources["receipts"][0]
        self.assertTrue(got["accepted"])
        self.assertEqual(got["guard"], "review_requested")
        self.assertEqual(got["facts"], {"ticket": "ACME-7"})
        self.assertEqual(got["sha256"], receipt.intent_sha256)
        self.assertEqual(got["command_id"], receipt.command_id)
        self.assertEqual([d["sha"] for d in sources["deliveries"]], ["a" * 40])
        self.assertTrue(sources["deliveries"][0]["accepted"])
        self.assertEqual(len(sources["decisions"]), 1)
        # `requested` is set by the 0013 trigger: the reader returns the
        # real registry, not only the milestone added by the bench.
        self.assertEqual([m["kind"] for m in sources["milestones"]],
                         ["requested", "delivered"])
        with self.refusal("verifier-invalid"):
            pr.read_sources(object())

    def test_read_sources_reads_a_fresh_observations_projection(self):
        from ameesh import process_approvals as approvals_mod
        from ameesh import process_observations as observations_mod
        lot = self.pinned(stage="implementation", state="build")
        row = self.row(lot)
        moment = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        with tempfile.TemporaryDirectory() as directory:
            journal = observations_mod.Journal(
                os.path.join(directory, "journal.jsonl"), create=True)
            journal.reload()
            journal.record(observations_mod.Observation(
                id="obs-deployed", kind="deployed", environment="staging",
                work_item_id=lot, app=str(row.get("app") or ""),
                artifact="a" * 40, source_revision="a" * 12, actor="",
                observed_at=moment,
                provenance={"kind": "staging-attestation",
                            "ref": "actif-a.json", "sha256": "d" * 64,
                            "attestation": "a" * 12,
                            "hosts": ["ameesh-agents-1"]},
                source_digest="d" * 64))
            source = observations_mod.Source(
                journal=os.path.join(directory, "journal.jsonl"),
                projection=os.path.join(directory, "projection.json"),
                authority="ima", journal_id="ima", repos=(), issues=(),
                staging_app=str(row.get("app") or ""))
            projection = observations_mod.build_projection(
                source, journal, now=moment,
                collector={"host": "bureau-goal"})
        context = pr.VerificationContext(
            db=self.db, work_item=row,
            command=self.command(lot, version=0), spec=self.spec,
            from_stage="implementation", to_stage="implementation")
        with mock.patch.object(
                approvals_mod, "local_projection",
                side_effect=approvals_mod.ProjectionError(
                    "projection-missing", "test bench")), \
                mock.patch.object(
                    observations_mod, "local_projection",
                    return_value=projection):
            sources = pr.read_sources(context)
        self.assertIn("observations", sources)
        self.assertEqual([item["id"] for item in sources["observations"]],
                         ["obs-deployed"])
        self.assertEqual(sources["observations"][0]["sha"], "a" * 40)

    def test_read_sources_names_the_reserved_verifiers(self):
        lot = self.pinned(stage="implementation", state="build")
        held = self.apply(self.command(lot, action="hold", hold="blocked",
                                       frm="", to="", guard=""))
        context = pr.VerificationContext(
            db=self.db, work_item=self.row(lot),
            command=self.command(lot, version=held.version), spec=self.spec,
            from_stage="implementation", to_stage="implementation")
        sources = pr.read_sources(context)
        self.assertEqual([r["guard"] for r in sources["receipts"]],
                         [pr.HOLD_VERIFIER])

    # -- real seam with slice C (#1924) ------------------------------------

    @unittest.skipUnless(pv is not None, "slice C (#1924) missing from main")
    def test_real_seam_with_slice_c(self):
        """With the 16 callables of C installed, the public API makes them talk.

        The real C reads the snapshot via `read_sources(context)`: advance
        guarded by `planning`, hold `blocked` and release by the reserved name
        `hold`, `waiting_human` on the `decisions` collection, and advance
        refused from a hold."""
        saved = pr.installed_verifiers()

        def restore():
            pr.INSTALLED_VERIFIERS.clear()
            pr.INSTALLED_VERIFIERS.update(saved)

        self.addCleanup(restore)
        names = pv.install_verifiers(pr)
        self.assertEqual(len(names), len(pv.GUARD_NAMES) + 2)
        self.assertIn(pr.HOLD_VERIFIER, names)
        self.assertIn(pr.TRANSFER_VERIFIER, names)      # #4007

        lot = self.pinned(stage="draft", state="intake")
        first = pr.apply_command(self.db, self.command(lot), spec=self.spec)
        self.assertEqual((first.state, first.workflow_stage),
                         ("build", "implementation"))

        held = pr.apply_command(self.db, self.command(
            lot, action="hold", hold="blocked", frm="", to="", guard="",
            version=first.version), spec=self.spec)
        self.assertEqual(held.state, "blocked")
        with self.refusal("held"):          # an advance does not lift the hold
            pr.apply_command(self.db, self.command(
                lot, frm="implementation", to="review", guard="review_requested",
                version=held.version), spec=self.spec)
        released = pr.apply_command(self.db, self.command(
            lot, action="release", frm="", to="", guard="",
            version=held.version), spec=self.spec)
        self.assertEqual(released.state, "build")

        request = self.request_open_for(lot)
        waiting = pr.apply_command(self.db, self.command(
            lot, action="hold", hold="waiting_human", frm="", to="", guard="",
            version=released.version), spec=self.spec)
        self.assertEqual(waiting.state, "waiting_human")
        with self.refusal("decision-pending"):   # the answer first
            pr.apply_command(self.db, self.command(
                lot, action="release", frm="", to="", guard="",
                version=waiting.version), spec=self.spec)
        self.db.execute("UPDATE agent_mailbox SET delivered_at = now(),"
                        " status = 'answered' WHERE id = %s", (request,))
        after = pr.apply_command(self.db, self.command(
            lot, action="release", frm="", to="", guard="",
            version=waiting.version), spec=self.spec)
        self.assertEqual(after.state, "build")

    @unittest.skipUnless(pv is not None, "tranche C (#1924) absente de main")
    def test_seam_reel_acteur_du_verdict_et_id_de_livraison(self):
        """#1994 : les deux coutures B -> C passent par le `read_sources` réel.

        (1) l'acteur du verdict est projeté : `work_completed` accepte une revue
        réelle (avant le correctif : `completion-review-missing`) ; (2) l'id de
        livraison est projeté en chaîne, comme la référence client : une
        livraison de code référencée par son id est trouvée (avant :
        `completion-evidence-missing`)."""
        saved = pr.installed_verifiers()

        def restore():
            pr.INSTALLED_VERIFIERS.clear()
            pr.INSTALLED_VERIFIERS.update(saved)

        self.addCleanup(restore)
        pv.install_verifiers(pr)

        sha = "c" * 40
        # Un lot de code : la livraison est référencée par son id (chaîne) et
        # la revue par son acteur — les deux coutures du même `read_sources`.
        code = self.pinned(stage="staging", state="merged")
        delivered = int(self.db.query(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor)"
            " VALUES (%s, 'delivered', %s, %s) RETURNING id",
            (code, sha, ACME_ACTOR))[0]["id"])
        self.db.execute(
            "INSERT INTO work_item_milestones (work_item_id, kind, sha, actor,"
            "                                verdict)"
            " VALUES (%s, 'verdict', %s, %s, 'ok')", (code, sha, "relecteur-1"))
        context = pr.VerificationContext(
            db=self.db, work_item=self.row(code),
            command=self.command(code, frm="staging", to="done",
                                 guard="work_completed"),
            spec=self.spec, from_stage="staging", to_stage="done")
        sources = pr.read_sources(context)
        verdicts = [m for m in sources["milestones"] if m["kind"] == "verdict"]
        self.assertEqual([m["actor"] for m in verdicts], ["relecteur-1"])
        self.assertEqual([d["id"] for d in sources["deliveries"]], [str(delivered)])
        completed = pr.apply_command(self.db, self.command(
            code, frm="staging", to="done", guard="work_completed",
            facts={"delivery": str(delivered)}), spec=self.spec)
        self.assertEqual(completed.state, "promoted")


@unittest.skipUnless(pw is not None, "slice A (#1918) missing from main")
class CompiledArtifactTest(PgTestCase):
    """The slice A artifact satisfies the protocol without adaptation."""

    def test_compiled_artifact_accepted_by_the_registry(self):
        document = {
            "schema": "process-workflow/1",
            "initial": "draft",
            "types": ["evolution"],
            "stages": [{"status": s.status, "core": s.core} for s in ACME_STAGES],
            "transitions": [{"from": t.source, "to": t.target, "guard": t.guard}
                            for t in ACME_TRANSITIONS],
            "projection": {
                "project": "acme", "status_field": "state",
                "product_field": "product",
                "issue_open": ["intake", "build", "qa", "merged"],
                "issue_completed": ["promoted"],
                "issue_not_planned": ["closed"],
            },
        }
        compiled = pw.compile_workflow(document, ref=ACME_REF, version=ACME_VERSION)
        self.db.execute(
            "INSERT INTO agent_registry (name, lease_owner, lease_epoch,"
            "                            lease_expires_at)"
            " VALUES ('acme-dev1', 'acme-runner-1', 4, now() + interval '1 hour')")
        row = self.db.query(
            "INSERT INTO work_items (title, state, app, workflow_ref, workflow_stage,"
            "                        workflow_version)"
            " VALUES ('acme : lot compilé', 'intake', 'acme', %s, 'draft', %s)"
            " RETURNING id", (ACME_REF, ACME_VERSION))
        command = pr.Command(
            work_item_id=int(row[0]["id"]), command_id=str(uuid.uuid4()),
            expected_version=0,
            authority=pr.Authority(actor="acme-dev1", source="runner", epoch=4,
                                   runner_id="acme-runner-1"),
            workflow_ref=ACME_REF, workflow_version=ACME_VERSION, action="advance",
            from_stage="draft", to_stage="implementation", guard="planning")
        receipt = pr._apply(
            self.db, command, spec=compiled,
            verifiers={"planning": lambda context: pr.Verification(ok=True)})
        self.assertEqual(receipt.state, "build")
        self.assertEqual(receipt.workflow_stage, "implementation")

    def test_two_targets_under_one_guard_chosen_by_to_stage(self):
        """#1379 : deux cibles distinctes sous une même garde.

        La carte de référence a `draft -> backlog` et `draft -> sprint` sous
        `planning`. Le compilateur accepte ces deux arêtes ; le registre
        choisit l'arête sur le couple exact (`to_stage`, `guard`) depuis
        `from_stage`, et refuse un couple non déclaré.
        """
        stages = (ACME_STAGES[0], _Stage("backlog", "intake"),
                  _Stage("sprint", "intake")) + ACME_STAGES[1:]
        transitions = ACME_TRANSITIONS + (
            _Transition("draft", "backlog", "planning"),
            _Transition("draft", "sprint", "planning"))
        document = {
            "schema": "process-workflow/1",
            "initial": "draft",
            "types": ["evolution"],
            "stages": [{"status": s.status, "core": s.core} for s in stages],
            "transitions": [{"from": t.source, "to": t.target, "guard": t.guard}
                            for t in transitions],
            "projection": {
                "project": "acme", "status_field": "state",
                "product_field": "product",
                "issue_open": ["intake", "build", "qa", "merged"],
                "issue_completed": ["promoted"],
                "issue_not_planned": ["closed"],
            },
        }
        compiled = pw.compile_workflow(document, ref=ACME_REF, version=ACME_VERSION)
        self.db.execute(
            "INSERT INTO agent_registry (name, lease_owner, lease_epoch,"
            "                            lease_expires_at)"
            " VALUES ('acme-dev1', 'acme-runner-1', 4, now() + interval '1 hour')")

        def advance(target, guard):
            row = self.db.query(
                "INSERT INTO work_items (title, state, app, workflow_ref,"
                "                        workflow_stage, workflow_version)"
                " VALUES ('acme : lot bi-cible', 'intake', 'acme', %s, 'draft', %s)"
                " RETURNING id", (ACME_REF, ACME_VERSION))
            command = pr.Command(
                work_item_id=int(row[0]["id"]), command_id=str(uuid.uuid4()),
                expected_version=0,
                authority=pr.Authority(actor="acme-dev1", source="runner", epoch=4,
                                       runner_id="acme-runner-1"),
                workflow_ref=ACME_REF, workflow_version=ACME_VERSION, action="advance",
                from_stage="draft", to_stage=target, guard=guard)
            return pr._apply(
                self.db, command, spec=compiled,
                verifiers={"planning": lambda context: pr.Verification(ok=True)})

        for target in ("backlog", "sprint"):
            with self.subTest(target=target):
                receipt = advance(target, "planning")
                self.assertEqual(receipt.state, "intake")
                self.assertEqual(receipt.workflow_stage, target)
        with self.assertRaises(pr.RegistryRefusal) as caught:
            advance("implementation", "stop_work")
        self.assertEqual("edge-undeclared", caught.exception.code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
