# SPDX-License-Identifier: AGPL-3.0-only
"""Pilot commands against real persisted train/fire state; no remote effects."""
import contextlib
import io
import json
import os
import types
from unittest import mock

from ameesh import train, train_cli, work
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

    def test_show_is_read_only_and_exports_whole_reviewable_plan(self):
        before = self.ledger.load()
        result = self.execute("show")
        self.assertEqual(result, dict(phase="ci", revision=before["revision"],
                                     plan=self.plan, fire=self.fire))
        self.assertEqual(self.ledger.load(), before)
        self.assertEqual(self.fires.ops.train_fire_read(self.ledger.key, self.plan["id"]), self.fire)

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
