# SPDX-License-Identifier: AGPL-3.0-only
"""Signed authority and atomic admission for a single named quota exception."""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import types
from unittest import mock

from ameesh import (account_exception as exceptions, account_turn, accounts,
                    authority, cost, decisions, registry, signing, storage)
from .test_l124_decisions import _Base as DecisionBase
from .test_l30_accounts import _Base as AccountBase, _dossier
from .support import FAKEBIN


class NamedExceptionTest(DecisionBase):
    def setUp(self):
        super().setUp()
        # Anchor the injected clock to the disposable database's clock: approval
        # views also check SQL now(). All cases use offsets from this fixture.
        self.now = float(self.db.query("select extract(epoch from clock_timestamp()) as ts")[0]["ts"])
        clock = mock.patch("time.time", return_value=self.now)
        self.clock = clock.start()
        self.addCleanup(clock.stop)
        ns_clock = mock.patch("time.time_ns", return_value=int(self.now * 1_000_000_000))
        ns_clock.start()
        self.addCleanup(ns_clock.stop)
        registry.upsert(self.db, "alice", harness="other", host=self.cfg.host)
        self.key, public, _ = signing.write_keypair(os.path.join(self.tmp, "keys"), "alice")
        authority.register_key(self.db, "alice", signing.read_public(public), role="owner")
        self.cfg = dataclasses.replace(self.cfg, accounts={"codex": [{
            "name": "primary", "path": _dossier(os.path.join(self.tmp, "account")),
            "cap": 0.90, "overage": "allowed", "check_credentials": False,
        }]})
        self.items = accounts.profiles(self.cfg, "codex")
        self.profile = self.items[0]
        self.worker = types.SimpleNamespace(
            cfg=self.cfg, db=self.db, name="dev1", agent={"harness": "codex"},
            _account=None, _account_override=None,
            _account_exception=None)
        self.book = cost.CostBook(state_dir=self.cfg.state_dir, db=self.db,
                                  tools={"dev1": "codex"})
        self.gauges = cost.Gauges([
            cost.Gauge("codex", "week", 0.91, self.now + 3600,
                       7 * 86400, read_at=self.now),
        ])
        patch = mock.patch.object(accounts, "gauges_of", side_effect=lambda *a, **k: self.gauges)
        patch.start()
        self.addCleanup(patch.stop)
        for table in ("account_turn_exceptions", "account_pauses", "account_identities",
                      "account_holds", "account_active", "account_switches",
                      "account_rate_events", "account_turn_starts", "account_turn_slots"):
            self.db.execute("delete from " + table)

    def token(self, **changes):
        token = dict(kind=exceptions.KIND, agent="dev1", harness="codex", account="primary",
                     turns=1, extra_points=15, expires_at=self.now + 600)
        token.update(changes)
        return token

    def signed(self, token=None, *, signed=True):
        view = self.ask(options=[], needs_signature=signed)
        self.answer(view["id"], exceptions.PREFIX + json.dumps(self.token() if token is None else token),
                    **({"key_path": self.key} if signed else {}))
        return view["id"]

    def prepare(self):
        identifier = self.signed()
        exceptions.register(self.db, identifier, now=self.now)
        self.assertEqual(account_turn.choose(self.worker, self.book), "")
        self.assertEqual(self.worker._account_exception, identifier)
        return identifier

    def consumed(self, identifier):
        return self.db.query("select consumed_at, consumed_host from account_turn_exceptions"
                             " where decision_id = %s", (identifier,))[0]

    def test_one_turn_then_pause_and_reregistration_cannot_rearm(self):
        identifier = self.prepare()
        self.assertEqual(account_turn.choose(self.worker, self.book), "")
        self.assertIsNone(self.consumed(identifier)["consumed_at"], "prechecks do not consume")
        opened = []
        self.assertTrue(exceptions.begin(self.worker, self.profile, lambda tx: opened.append(tx) or True))
        self.assertEqual(len(opened), 1)
        self.assertEqual(self.consumed(identifier)["consumed_host"], self.cfg.host)
        exceptions.register(self.db, identifier, now=self.now)
        self.assertIsNone(exceptions.preview(self.worker, self.items, self.book, now=self.now))
        self.assertTrue(account_turn.choose(self.worker, self.book))
        self.assertIsNone(self.worker._account_exception)

    def test_other_agent_and_other_account_do_not_benefit(self):
        self.prepare()
        other = types.SimpleNamespace(**vars(self.worker))
        other.name = "orch"
        self.assertIsNone(exceptions.preview(other, self.items, self.book, now=self.now))
        other._account_exception = self.worker._account_exception
        self.assertFalse(exceptions.begin(other, self.profile, lambda tx: self.fail("wrong agent")))
        wrong = dataclasses.replace(self.profile, name="secondary")
        self.assertFalse(exceptions.begin(self.worker, wrong, lambda tx: self.fail("wrong account")))

    def test_expired_token_refused_at_registration_and_start(self):
        expired = self.signed(self.token(expires_at=self.now))
        with self.assertRaisesRegex(exceptions.ExceptionError, "expired"):
            exceptions.register(self.db, expired, now=self.now)
        identifier = self.prepare()
        self.clock.return_value = self.now + 601
        self.assertFalse(exceptions.begin(self.worker, self.profile, lambda tx: self.fail("expired")))
        self.assertIsNone(self.consumed(identifier)["consumed_at"])

    def test_unsigned_or_tampered_decision_refused(self):
        identifier = self.signed(signed=False)
        with self.assertRaisesRegex(exceptions.ExceptionError, "signature"):
            exceptions.register(self.db, identifier, now=self.now)
        identifier = self.signed()
        self.db.execute("update agent_mailbox set payload = jsonb_set(payload,"
                        " '{decision,answer,text}', %s::jsonb) where id = %s",
                        (json.dumps(json.dumps(self.token(extra_points=99))), identifier))
        with self.assertRaisesRegex(exceptions.ExceptionError, "signature"):
            exceptions.register(self.db, identifier, now=self.now)

    def test_exceeded_bound_and_saturation_refused(self):
        self.prepare()
        for used in (0.95, 1.0, float("nan")):
            with self.subTest(used=used):
                self.gauges[0] = dataclasses.replace(self.gauges[0], used=used)
                self.assertFalse(exceptions.begin(self.worker, self.profile, lambda tx: self.fail("bound")))

    def test_failed_begin_does_not_consume_and_rollback_restores_token(self):
        identifier = self.prepare()
        self.assertFalse(exceptions.begin(self.worker, self.profile, lambda tx: False))
        self.assertIsNone(self.consumed(identifier)["consumed_at"])
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.db.transaction() as tx:
                self.assertTrue(exceptions.begin(self.worker, self.profile, lambda tx: True, db=tx))
                raise RuntimeError("abort")
        self.assertIsNone(self.consumed(identifier)["consumed_at"])
        self.assertTrue(exceptions.begin(self.worker, self.profile, lambda tx: True))
        self.assertFalse(exceptions.begin(self.worker, self.profile, lambda tx: self.fail("replay")))

    def test_revoked_owner_key_refuses_start(self):
        self.prepare()
        self.db.execute("update agent_registry set key_revoked_at = now() where name = 'alice'")
        self.assertFalse(exceptions.begin(self.worker, self.profile, lambda tx: self.fail("revoked")))

    def test_shared_pauses_and_active_account_remain_unchanged(self):
        self.prepare()
        before = storage.of(self.db).accounts.active(self.cfg.host, "codex")
        pauses = storage.of(self.db).accounts.account_pauses("codex")
        self.assertTrue(pauses)
        self.assertTrue(exceptions.begin(self.worker, self.profile, lambda tx: True))
        self.assertEqual(storage.of(self.db).accounts.account_pauses("codex"), pauses)
        self.assertEqual(storage.of(self.db).accounts.active(self.cfg.host, "codex"), before)

    def test_newer_shared_observation_refuses_exception(self):
        self.prepare()
        storage.of(self.db).accounts.publish_account_pause(
            "codex", "primary", self.now + 3600, self.now + 1, "newer hard cap")
        self.assertFalse(exceptions.begin(self.worker, self.profile, lambda tx: self.fail("stale")))

    def test_stale_or_missing_gauge_refused(self):
        self.prepare()
        for read_at in (None, self.now - 3601):
            self.gauges[0] = dataclasses.replace(self.gauges[0], read_at=read_at)
            self.assertFalse(exceptions.begin(self.worker, self.profile, lambda tx: self.fail("stale")))
        self.gauges.clear()
        self.assertFalse(exceptions.begin(self.worker, self.profile, lambda tx: self.fail("missing")))

    def test_invalid_structured_fields_refused(self):
        for change in ({"turns": 2}, {"turns": True}, {"extra_points": -1},
                       {"extra_points": float("nan")}, {"extra_points": True},
                       {"account": ""}, {"expires_at": True}):
            with self.subTest(change=change):
                identifier = self.signed(self.token(**change))
                with self.assertRaises(exceptions.ExceptionError):
                    exceptions.register(self.db, identifier, now=self.now)

    def test_cli_registers_only_a_signed_decision(self):
        identifier = self.signed()
        accepted = self.mesh("accounts", "exception", str(identifier))
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertIn("one-turn exception", accepted.stdout)
        refused = self.mesh("accounts", "exception", str(self.signed(signed=False)))
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("signature rejected", refused.stderr)

    def test_profile_change_between_preview_and_start_refused(self):
        self.prepare()
        self.worker.cfg = dataclasses.replace(self.cfg, accounts={"codex": [{
            "name": "primary", "path": self.profile.path, "cap": 0.5,
            "check_credentials": False}]})
        self.assertFalse(exceptions.begin(self.worker, self.profile, lambda tx: self.fail("profile changed")))

    def test_two_hosts_cannot_consume_the_same_token(self):
        identifier = self.prepare()
        barrier = threading.Barrier(2)
        outcomes, errors = [], []

        def attempt(host):
            connection = self.connect()
            worker = types.SimpleNamespace(**vars(self.worker))
            worker.db = connection
            worker.cfg = dataclasses.replace(self.cfg, host=host)
            try:
                barrier.wait(timeout=10)
                outcomes.append(exceptions.begin(worker, self.profile, lambda tx: True))
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt, args=(host,)) for host in ("host-a", "host-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(errors)
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertIn(self.consumed(identifier)["consumed_host"], ("host-a", "host-b"))

    def test_short_window_refusal_preserves_token_and_admitted_turn_consumes(self):
        from ameesh import account_window
        identifier = self.prepare()
        account_window.record_rate_limit(self.db, "codex", "primary", self.cfg.host, "dev1")
        callback = lambda tx: exceptions.begin(self.worker, self.profile, lambda connection: True, db=tx)
        admission = account_window.claim_start(
            self.db, "codex", "primary", self.cfg.host, "dev1", self.profile, begin=callback)
        self.assertFalse(admission.ok)
        self.assertIsNone(self.consumed(identifier)["consumed_at"])
        self.db.execute("delete from account_rate_events")
        admission = account_window.claim_start(
            self.db, "codex", "primary", self.cfg.host, "dev1", self.profile, begin=callback)
        self.assertTrue(admission.ok)
        self.assertTrue(admission.begun)
        self.assertIsNotNone(self.consumed(identifier)["consumed_at"])

    def test_authentication_and_conflicting_identity_remain_guards(self):
        self.prepare()
        with mock.patch.object(exceptions.auth_failure, "failing", return_value={"primary": {}}):
            self.assertFalse(exceptions.begin(self.worker, self.profile, lambda tx: self.fail("auth")))
        storage.of(self.db).accounts.publish_identity(self.cfg.host, "codex", "primary", "different")
        self.assertFalse(exceptions.begin(self.worker, self.profile, lambda tx: self.fail("identity")))

    def test_runner_launches_one_fake_harness_turn_then_pauses(self):
        identifier = self.signed()
        exceptions.register(self.db, identifier, now=self.now)
        _runner, worker = AccountBase._worker(self, "dev1", "codex", self.cfg.accounts)
        self.assertTrue(worker.budget_ok())
        self.assertIsNone(self.consumed(identifier)["consumed_at"])
        registry.set_pending_prompt(self.db, "dev1", "Complete the fixture turn.")
        turn_log = os.path.join(self.tmp, "fixture-turns.jsonl")
        env = {"AMEESH_BIN_DIR": FAKEBIN, "AMEESH_TEST_LOG": turn_log,
               "PATH": AccountBase._python_du_banc(self) + os.pathsep + os.environ.get("PATH", "")}
        with mock.patch.dict(os.environ, env):
            spec = worker.pick()
            self.assertIsNotNone(spec)
            self.assertTrue(worker.run_turn(spec))
        self.assertIsNotNone(self.consumed(identifier)["consumed_at"])
        self.assertFalse(worker.budget_ok())
        with open(turn_log, encoding="utf-8") as stream:
            self.assertEqual(len(stream.readlines()), 1)

    def test_running_turn_uses_signed_bound_and_preserves_shared_pause(self):
        identifier = self.signed()
        exceptions.register(self.db, identifier, now=self.now)
        _runner, worker = AccountBase._worker(self, "dev1", "codex", self.cfg.accounts)
        self.assertTrue(worker.budget_ok())
        self.assertTrue(exceptions.begin(worker, self.profile, lambda tx: True))
        self.assertIsNone(worker.account_ceiling_due(now=self.now))
        self.assertTrue(storage.of(self.db).accounts.account_pauses("codex"))
        storage.of(self.db).accounts.publish_account_pause(
            "codex", "primary", self.now + 3600, self.now + 1, "newer observation")
        worker._account_ceiling_next = 0
        self.assertIn("older than", worker.account_ceiling_due(now=self.now))
        storage.of(self.db).accounts.release_account_pause("codex", "primary", self.now + 1)
        self.gauges[0] = dataclasses.replace(self.gauges[0], used=0.95)
        worker._account_ceiling_next = 0
        self.assertIn("bound reached", worker.account_ceiling_due(now=self.now))
        self.gauges[0] = dataclasses.replace(self.gauges[0], used=0.91)
        worker._account_ceiling_next = 0
        self.clock.return_value = self.now + 601
        self.assertIn("no longer valid", worker.account_ceiling_due(now=self.now + 601))
