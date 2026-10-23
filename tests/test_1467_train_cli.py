# SPDX-License-Identifier: AGPL-3.0-only
"""Pilot commands against real persisted train/fire state; no remote effects."""
import contextlib
import io
import json
import os
import types
from unittest import mock

from ameesh import train, train_cli, train_fire, train_store, work
from . import test_1467_train_fire as fixture
from .support import PgTestCase


class TrainCommandTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.db.execute("TRUNCATE merge_locks RESTART IDENTITY CASCADE")
        self.ledger, self.epoch, self.plan, self.document, self.fires, self.fire = (
            fixture.TrainFireTest.prepare(self))
        self.project = types.SimpleNamespace(name="fire-test", target="main")

    def execute(self, action, **kwargs):
        return train_cli.execute(None, self.db, self.project, action, **kwargs)

    def unfired(self):
        """A persisted plan with no fire of its own, and a free target lease."""
        item = dict(self.plan, id="f" * 64, nonce="a" * 64,
                    head="f" * 40, tree="f" * 40)
        document = self.ledger.save(dict(self.document, phase="publishing", plan=item),
                                    self.document["revision"], self.epoch)
        self.ledger.release(self.epoch)
        return item, document

    def test_show_is_read_only_and_exports_whole_reviewable_plan(self):
        before = self.ledger.load()
        result = self.execute("show")
        self.assertEqual(result, dict(phase="ci", revision=before["revision"],
                                     plan=self.plan, fire=self.fire,
                                     enrollment_deferred=[],
                                     enrollment_checked_ts=None))
        self.assertEqual(self.ledger.load(), before)
        self.assertEqual(self.fires.ops.train_fire_read(self.ledger.key, self.plan["id"]), self.fire)

    def test_show_names_each_deferred_enrollment_without_a_plan(self):
        # #3139: a train that could not enroll a single candidate has no plan
        # but still carries the per-lot refusal the pilot must read.
        deferred = [dict(lot=3012, branch="lot/3012-election-standby-brief",
                         frozen_sha="a" * 40, frozen_id=16279,
                         reason="prior freeze c5d4348160c5 is absent",
                         checked_ts=1792759301.5)]
        self.ledger.save(dict(schema=train.SCHEMA, source="github.com/o/r",
                              target="main", phase="empty",
                              enrollment_deferred=deferred,
                              enrollment_checked_ts=1792759301.5),
                         self.document["revision"], self.epoch)
        result = self.execute("show")
        self.assertEqual(result["plan"], {})
        self.assertIsNone(result["fire"])
        self.assertEqual(result["enrollment_checked_ts"], 1792759301.5)
        self.assertEqual(result["enrollment_deferred"], deferred)

    def test_withdrawal_and_regrant_bind_session_and_exact_receipt(self):
        with mock.patch.object(work, "corrector", return_value="pilot") as guard:
            revoked = self.execute("withdraw", manifest=self.plan, receipt=self.fire["id"])
            self.assertEqual(revoked["state"], "revoked")
            grant = self.execute("give", manifest=self.plan)
            self.assertGreater(grant["id"], revoked["id"])
            with self.assertRaisesRegex(train.TrainError, "current exact fire"):
                self.execute("withdraw", manifest=self.plan, receipt=revoked["id"])
        self.assertTrue(all(call == mock.call(None, self.db, None, history=False)
                            for call in guard.call_args_list))

    def test_reviewed_file_cannot_grant_replacement_plan_or_diagnostic(self):
        with mock.patch.object(work, "corrector", return_value="pilot"):
            self.execute("withdraw", manifest=self.plan, receipt=self.fire["id"])
            new = dict(self.plan, nonce="f" * 64)
            self.ledger.save(dict(self.document, plan=new), self.document["revision"], self.epoch)
            with self.assertRaisesRegex(train.TrainError, "plan changed"):
                self.execute("give", manifest=self.plan)
            with self.assertRaisesRegex(train.TrainError, "non-diagnostic"):
                self.execute("give", manifest=dict(new, diagnostic=True))
        self.assertEqual(self.fires.ops.train_fire_read(self.ledger.key, self.plan["id"])["state"],
                         "revoked")

    def test_cli_reads_manifest_and_closes_connection_without_runtime(self):
        path = os.path.join(self.tmp, "manifest.json")
        with open(path, "w") as output:
            json.dump(self.plan, output)
        cfg = types.SimpleNamespace()
        db = mock.Mock(wraps=self.db)
        with mock.patch.object(train_cli.config, "load", return_value=cfg), \
                mock.patch.object(train_cli.radar, "settings_of", return_value=types.SimpleNamespace(
                    projects={"fire-test": self.project})), \
                mock.patch.object(train_cli.db_mod, "open_db", return_value=db), \
                mock.patch.object(work, "corrector", return_value="pilot"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = train_cli.main(["--project", "fire-test", "withdraw", "--manifest", path,
                                     "--receipt", str(self.fire["id"])])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "revoked")
        db.close.assert_called_once_with()

    def test_abandon_terminates_an_unfired_plan_and_frees_the_lease(self):
        item, document = self.unfired()
        with mock.patch.object(work, "corrector", return_value="pilot") as guard:
            result = self.execute("abandon", manifest=item, reason="incident")
        self.assertEqual(result["phase"], "invalidated")
        self.assertEqual(result["plan"], item)
        self.assertIsNone(result["fire"])
        self.assertEqual(result["abandoned"], dict(
            actor="pilot", at_ts=result["abandoned"]["at_ts"], reason="incident",
            previous_phase="publishing", plan=item["id"], fire=None))
        state = self.ledger.load()
        self.assertEqual(state["phase"], "invalidated")
        self.assertEqual(state["revision"], document["revision"] + 1)
        self.assertEqual(state["abandoned"], result["abandoned"])
        self.assertEqual(self.db.query(
            "SELECT owner FROM merge_locks WHERE target = %s", (self.ledger.key,)),
            [{"owner": None}])
        # a new plan can take the target lease at once
        epoch = self.ledger.acquire(None)
        self.assertIsNotNone(epoch)
        self.ledger.release(epoch)
        self.assertEqual(guard.call_args, mock.call(None, self.db, None, history=False))
        # show exposes the terminal gesture and no fire
        shown = self.execute("show")
        self.assertEqual((shown["phase"], shown["abandoned"], shown["fire"]),
                         ("invalidated", result["abandoned"], None))

    def test_abandon_refuses_a_fired_plan_without_its_receipt(self):
        before = self.ledger.load()
        with mock.patch.object(work, "corrector", return_value="pilot"):
            with self.assertRaisesRegex(train.TrainError, "fire receipt"):
                self.execute("abandon", manifest=self.plan)
        self.assertEqual(self.ledger.load(), before)
        self.assertEqual(self.db.query(
            "SELECT owner FROM merge_locks WHERE target = %s", (self.ledger.key,)),
            [{"owner": self.ledger.owner}])

    def test_abandon_refuses_an_uncertain_promotion(self):
        self.ledger.save(dict(self.document, phase="promoting"),
                         self.document["revision"], self.epoch)
        with mock.patch.object(work, "corrector", return_value="pilot"):
            with self.assertRaisesRegex(train.TrainError, "uncertain promotion"):
                self.execute("abandon", manifest=self.plan)

    def test_abandon_refuses_anything_but_the_exact_persisted_plan(self):
        item, _ = self.unfired()
        with mock.patch.object(work, "corrector", return_value="pilot"):
            with self.assertRaisesRegex(train.TrainError, "exact persisted plan"):
                self.execute("abandon", manifest=dict(item, nonce="0" * 64))
        self.assertEqual(self.ledger.load()["phase"], "publishing")
        with mock.patch.object(work, "corrector", return_value="pilot"):
            self.execute("abandon", manifest=item)
            with self.assertRaisesRegex(train.TrainError, "already terminal"):
                self.execute("abandon", manifest=item)

    def test_abandon_accepts_a_revoked_fire(self):
        # A withdrawn fire authorizes nothing; the plan is no longer "fired".
        with mock.patch.object(work, "corrector", return_value="pilot"):
            self.fires.withdraw(self.plan, self.fire["id"], actor="pilot")
        self.ledger.save(dict(self.document, phase="publishing"),
                         self.document["revision"], self.epoch)
        self.ledger.release(self.epoch)
        with mock.patch.object(work, "corrector", return_value="pilot"):
            result = self.execute("abandon", manifest=self.plan)
        self.assertEqual(result["phase"], "invalidated")
        self.assertEqual(result["abandoned"]["fire"], self.fire["id"])

    def test_abandon_waits_for_a_live_foreign_lease_then_takes_the_expired_one(self):
        item, _ = self.unfired()
        epoch = self.ledger.acquire(None)
        self.assertIsNotNone(epoch)
        try:
            with mock.patch.object(work, "corrector", return_value="pilot"):
                with self.assertRaisesRegex(train.TrainError, "live train"):
                    self.execute("abandon", manifest=item)
            self.db.execute("UPDATE merge_locks SET expires_at = clock_timestamp()"
                            " - interval '1 second' WHERE target = %s", (self.ledger.key,))
            with mock.patch.object(work, "corrector", return_value="pilot"):
                result = self.execute("abandon", manifest=item)
            self.assertEqual(result["phase"], "invalidated")
            self.assertEqual(self.db.query(
                "SELECT owner FROM merge_locks WHERE target = %s", (self.ledger.key,)),
                [{"owner": None}])
        finally:
            self.ledger.release(epoch)

    def test_cli_abandon_reads_manifest_and_closes_connection_without_runtime(self):
        item, _ = self.unfired()
        path = os.path.join(self.tmp, "manifest.json")
        with open(path, "w") as output:
            json.dump(item, output)
        cfg = types.SimpleNamespace()
        db = mock.Mock(wraps=self.db)
        with mock.patch.object(train_cli.config, "load", return_value=cfg), \
                mock.patch.object(train_cli.radar, "settings_of", return_value=types.SimpleNamespace(
                    projects={"fire-test": self.project})), \
                mock.patch.object(train_cli.db_mod, "open_db", return_value=db), \
                mock.patch.object(work, "corrector", return_value="pilot"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = train_cli.main(["--project", "fire-test", "abandon", "--manifest", path,
                                     "--reason", "incident"])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["phase"], "invalidated")
        db.close.assert_called_once_with()

    def test_storage_abandon_refuses_a_fire_granted_after_the_command_read(self):
        # #3138: the command reads the state before taking the lease; a
        # concurrent grant must not slip between that read and the terminal
        # write. The storage rechecks the fire under the target row lock.
        item, document = self.unfired()
        epoch = self.ledger.acquire(None)
        self.assertIsNotNone(epoch)
        try:
            with mock.patch.object(work, "corrector", return_value="pilot"):
                self.fires.give(item, actor="pilot")
            terminal = dict(document, phase="invalidated", abandoned={"actor": "pilot"})
            with self.assertRaisesRegex(train.TrainError, "fire changed"):
                self.ledger.abandon(terminal, document["revision"], epoch)
            self.assertEqual(self.ledger.load()["phase"], "publishing")
        finally:
            self.ledger.release(epoch)

    def test_abandon_command_refuses_a_grant_committed_after_its_read(self):
        # B1 cx2 (#3138): the command checks the fire, then takes the lease.
        # A give that commits in that window must not lead to invalidating an
        # authorized plan: the terminal write rechecks under the SQL lock that
        # give itself takes.
        item, document = self.unfired()
        real_acquire = train_store.Ledger.acquire
        granted = []
        def grant_then_acquire(ledger, epoch):
            if not granted:
                granted.append(True)
                with mock.patch.object(work, "corrector", return_value="pilot"):
                    train_fire.Fires(None, ledger).give(item, actor="pilot")
            return real_acquire(ledger, epoch)
        with mock.patch.object(train_store.Ledger, "acquire", grant_then_acquire), \
                mock.patch.object(work, "corrector", return_value="pilot"):
            with self.assertRaisesRegex(train.TrainError, "fire changed"):
                self.execute("abandon", manifest=item)
        self.assertEqual(self.ledger.load()["phase"], "publishing")
        self.assertEqual(self.db.query(
            "SELECT state FROM integration_train_fires WHERE plan_id = %s", (item["id"],)),
            [{"state": "active"}])
